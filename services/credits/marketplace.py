"""
Marketplace endpoints: list surplus credits, browse, cancel, purchase.

Owner-only (spec: 'Credits & Marketplace' is an Owner page) — listing and
buying move account value, so we hold them to the same bar Billing holds
refunds and plan changes to.

The purchase is the ACID centerpiece: claiming the listing, the seller's
debit row, and the buyer's credit row all commit in ONE transaction — either
the transfer fully happens or nothing does. Ordering inside the transaction:

  1. UPDATE ... SET status='sold' WHERE id=? AND status='open' — an
     optimistic claim; rowcount 0 means someone else bought it (or it was
     canceled) and we stop with 409. Under Postgres this row lock also
     serializes concurrent buyers of the same listing.
  2. Re-check the seller's balance (listings don't escrow credits — the
     seller may have spent them since listing). Insufficient -> rollback,
     which also releases the claim from step 1, and 409.
  3. INSERT the two ledger rows, COMMIT.

Funds release to the seller: the sale (price_cents, buyer, seller) is
recorded on the listing row and carried on both credits.* events. Actually
moving the money is Billing's job (spec: escrow payment intent via Billing)
— Billing does not yet expose a transfer/payout API, so settlement is
deferred to that integration; the credit transfer itself is final here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

import database
import ledger
import schemas
from models import LedgerEntry, MarketplaceListing, utcnow
from routes import current_claims, emit_balance_events, require_owner

router = APIRouter(tags=["marketplace"])


def _listing_view(listing: MarketplaceListing) -> dict:
    return {
        "listing_id": listing.id,
        "seller_account_id": listing.seller_account_id,
        "credits_amount": listing.credits_amount,
        "price_cents": listing.price_cents,
        "status": listing.status,
        "buyer_account_id": listing.buyer_account_id,
        "created_at": listing.created_at.isoformat(),
        "sold_at": listing.sold_at.isoformat() if listing.sold_at else None,
    }


@router.get("/credits/marketplace/listings")
def browse_listings(claims: dict = Depends(current_claims), db: Session = Depends(database.get_db)) -> dict:
    """All OPEN listings platform-wide — any member may browse (buying is
    owner-only, but the storefront is public to authenticated users)."""
    rows = db.scalars(
        select(MarketplaceListing)
        .where(MarketplaceListing.status == "open")
        .order_by(MarketplaceListing.created_at.desc())
    ).all()
    return {"listings": [_listing_view(listing) for listing in rows]}


@router.post("/credits/marketplace/listings", status_code=201)
def create_listing(
    body: schemas.ListingCreate,
    claims: dict = Depends(require_owner),
    db: Session = Depends(database.get_db),
) -> dict:
    """List surplus credits for sale. The seller must have the credits NOW —
    a sanity gate against listing thin air. (They can still spend them before
    a buyer shows up; the purchase transaction re-checks and rejects then.)"""
    account_id = claims["account_id"]
    available = ledger.balance(db, account_id)
    if body.credits_amount > available:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot list {body.credits_amount} credits: balance is {available}",
        )
    listing = MarketplaceListing(
        seller_account_id=account_id,
        credits_amount=body.credits_amount,
        price_cents=body.price_cents,
        created_by_user_id=claims["sub"],
    )
    db.add(listing)
    db.commit()
    return _listing_view(listing)


@router.delete("/credits/marketplace/listings/{listing_id}")
def cancel_listing(
    listing_id: str,
    claims: dict = Depends(require_owner),
    db: Session = Depends(database.get_db),
) -> dict:
    listing = db.get(MarketplaceListing, listing_id)
    if listing is None or listing.seller_account_id != claims["account_id"]:
        raise HTTPException(status_code=404, detail="Listing not found")
    # Same optimistic guard as purchase: only an OPEN listing can be canceled,
    # and a concurrent purchase that already claimed it wins.
    result = db.execute(
        update(MarketplaceListing)
        .where(MarketplaceListing.id == listing_id, MarketplaceListing.status == "open")
        .values(status="canceled")
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Listing is {listing.status}, not open")
    db.commit()
    db.refresh(listing)
    return _listing_view(listing)


@router.post("/credits/marketplace/listings/{listing_id}/purchase", status_code=201)
def purchase_listing(
    listing_id: str,
    claims: dict = Depends(require_owner),
    db: Session = Depends(database.get_db),
) -> dict:
    buyer_account_id = claims["account_id"]
    listing = db.get(MarketplaceListing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    if listing.seller_account_id == buyer_account_id:
        raise HTTPException(status_code=409, detail="Cannot buy your own listing")

    # Step 1 — claim the listing (fails if already sold/canceled).
    result = db.execute(
        update(MarketplaceListing)
        .where(MarketplaceListing.id == listing_id, MarketplaceListing.status == "open")
        .values(status="sold", buyer_account_id=buyer_account_id, sold_at=utcnow())
    )
    if result.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Listing is {listing.status}, not open")

    # Step 2 — the seller must still have the credits (no escrow at list time).
    seller_id = listing.seller_account_id
    seller_before = ledger.balance(db, seller_id)
    if listing.credits_amount > seller_before:
        db.rollback()  # releases the claim from step 1 — listing stays open
        raise HTTPException(status_code=409, detail="Seller no longer has enough credits")

    # Step 3 — both sides of the transfer, committed with the claim as ONE txn.
    buyer_before = ledger.balance(db, buyer_account_id)
    seller_entry = LedgerEntry(
        account_id=seller_id,
        amount=-listing.credits_amount,
        entry_type="marketplace_debit",
        listing_id=listing.id,
        counterparty_account_id=buyer_account_id,
        money_amount_cents=listing.price_cents,
        reason="marketplace sale",
    )
    buyer_entry = LedgerEntry(
        account_id=buyer_account_id,
        amount=listing.credits_amount,
        entry_type="marketplace_credit",
        listing_id=listing.id,
        counterparty_account_id=seller_id,
        money_amount_cents=listing.price_cents,
        reason="marketplace purchase",
        created_by_user_id=claims["sub"],
    )
    db.add_all([seller_entry, buyer_entry])
    db.commit()

    seller_after = seller_before - listing.credits_amount
    buyer_after = buyer_before + listing.credits_amount
    # Post-commit fan-out: debited (seller, may cross low-balance) + credited (buyer).
    emit_balance_events(seller_id, seller_entry, seller_before, seller_after)
    emit_balance_events(buyer_account_id, buyer_entry, buyer_before, buyer_after)

    db.refresh(listing)
    return {
        **_listing_view(listing),
        "buyer_balance": buyer_after,
        "credits_transferred": listing.credits_amount,
    }
