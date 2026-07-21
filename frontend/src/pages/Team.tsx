import { useState, type FormEvent } from "react";
import { Copy, Crown, MailPlus, Shield, UserMinus, UserRound, Users } from "lucide-react";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card, CardHeader } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { Field, Input, Select } from "../components/ui/Input";
import { ConfirmDialog, Modal } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { Table, type Column } from "../components/ui/Table";
import { useApi } from "../hooks/useApi";
import { useAuth } from "../hooks/useAuth";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { userApi } from "../lib/api/endpoints";
import type { AccountMember, AssignableRole, InviteCreated } from "../lib/api/types";
import { formatDateTime, shortId } from "../lib/format";

const ROLE_OPTIONS: { value: AssignableRole; label: string; hint: string }[] = [
  { value: "member", label: "Member", hint: "Can generate, draft and schedule content." },
  { value: "admin", label: "Admin", hint: "Also manages members and invites." },
];

/**
 * Spec §4 Owner-Only Pages: "Team Management — invite members, assign/change
 * roles, remove members." Every destructive step goes through a confirmation
 * (§4 Cross-Cutting).
 *
 * Two authorization rules from the User service shape this UI, so the screen
 * never offers an action the API will refuse:
 *   - the OWNER row is immutable — ownership is fixed at account creation, so
 *     it has no role selector and no remove button;
 *   - only the owner may promote someone to admin or manage a fellow admin;
 *     an admin can only act on plain members.
 */
export default function Team() {
  const { claims, role, activeAccount } = useAuth();
  const { toast } = useToast();
  const accountId = claims?.account_id ?? "";

  const [inviteOpen, setInviteOpen] = useState(false);
  const [lastInvite, setLastInvite] = useState<InviteCreated | null>(null);
  const [removing, setRemoving] = useState<AccountMember | null>(null);
  const [changing, setChanging] = useState<{ member: AccountMember; role: AssignableRole } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);

  const members = useApi(
    () => (accountId ? userApi.members(accountId) : Promise.resolve(null)),
    [accountId],
  );

  const isOwner = role === "owner";
  const rows = members.data?.members ?? [];
  const isIndividual = activeAccount?.type === "individual";

  /** Mirrors services/user/routes.py `_manageable_target`. */
  function canManage(member: AccountMember): boolean {
    if (member.role === "owner") return false;
    if (member.user_id === claims?.sub) return false; // no self-demotion/removal
    if (!isOwner && member.role === "admin") return false;
    return true;
  }

  async function applyRoleChange() {
    if (!changing) return;
    setBusy(true);
    try {
      await userApi.updateMemberRole(accountId, changing.member.user_id, changing.role);
      toast("success", "Role updated", `Now ${changing.role}.`);
      setChanging(null);
      members.reload();
    } catch (err) {
      toast("error", "Couldn't update role", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  async function removeMember() {
    if (!removing) return;
    setBusy(true);
    try {
      await userApi.removeMember(accountId, removing.user_id);
      toast("success", "Member removed");
      setRemoving(null);
      members.reload();
    } catch (err) {
      toast("error", "Couldn't remove member", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<AccountMember>[] = [
    {
      key: "user",
      header: "Member",
      render: (member) => (
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-full bg-surface-3 text-ink-faint">
            {member.role === "owner" ? <Crown size={13} /> : <UserRound size={13} />}
          </span>
          <div className="min-w-0">
            <p className="truncate font-mono text-xs text-ink">{shortId(member.user_id, 14)}</p>
            {member.user_id === claims?.sub && (
              <p className="text-2xs text-ink-faint">That's you</p>
            )}
          </div>
        </div>
      ),
    },
    {
      key: "role",
      header: "Role",
      render: (member) =>
        canManage(member) ? (
          <Select
            aria-label={`Role for ${member.user_id}`}
            value={member.role}
            className="h-8 w-32 text-xs"
            onChange={(e) =>
              setChanging({ member, role: e.target.value as AssignableRole })
            }
          >
            {ROLE_OPTIONS.map((option) => (
              <option
                key={option.value}
                value={option.value}
                // Only the owner can mint admins (User service refuses
                // otherwise) — so don't even offer it to an admin.
                disabled={option.value === "admin" && !isOwner}
              >
                {option.label}
              </option>
            ))}
          </Select>
        ) : (
          <Badge tone={member.role === "owner" ? "accent" : "neutral"} className="capitalize">
            {member.role}
          </Badge>
        ),
    },
    {
      key: "joined",
      header: "Joined",
      render: (member) => (
        <span className="text-xs text-ink-faint">{formatDateTime(member.joined_at)}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (member) =>
        canManage(member) ? (
          <Button
            variant="ghost"
            size="sm"
            icon={<UserMinus size={13} />}
            onClick={() => setRemoving(member)}
          >
            Remove
          </Button>
        ) : null,
    },
  ];

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Team"
        subtitle={
          isIndividual
            ? "This is an individual account — create a team account to work with others."
            : "Invite teammates, set their roles, and manage access to this account."
        }
        actions={
          <Button
            icon={<MailPlus size={15} />}
            disabled={isIndividual}
            onClick={() => setInviteOpen(true)}
          >
            Invite member
          </Button>
        }
      />

      <Card padded={false}>
        <div className="px-5 pt-5">
          <CardHeader
            title="Members"
            subtitle={
              rows.length > 0
                ? `${rows.length} ${rows.length === 1 ? "person" : "people"} in ${
                    activeAccount?.name ?? "this account"
                  }`
                : undefined
            }
          />
        </div>
        {members.error ? (
          <ErrorState error={members.error} onRetry={members.reload} />
        ) : (
          <Table
            columns={columns}
            rows={rows}
            rowKey={(member) => member.user_id}
            loading={members.loading}
            empty={
              <EmptyState
                icon={<Users size={18} />}
                title="No members yet"
                body="Invite someone by email — they'll join as soon as they accept."
              />
            }
          />
        )}
      </Card>

      {isIndividual && (
        <Card className="mt-4">
          <EmptyState
            icon={<Users size={18} />}
            title="Individual accounts have exactly one member"
            body="Spin up a team account from the account switcher, then invite people into it."
          />
        </Card>
      )}

      <InviteModal
        open={inviteOpen}
        accountId={accountId}
        isOwner={isOwner}
        onClose={() => setInviteOpen(false)}
        onInvited={(invite) => {
          setInviteOpen(false);
          setLastInvite(invite);
          members.reload();
        }}
      />

      <InviteSentModal invite={lastInvite} onClose={() => setLastInvite(null)} />

      <ConfirmDialog
        open={changing !== null}
        onClose={() => setChanging(null)}
        onConfirm={() => void applyRoleChange()}
        title="Change this member's role"
        busy={busy}
        danger={false}
        confirmLabel={`Make ${changing?.role ?? "member"}`}
        body={
          changing?.role === "admin"
            ? "Admins can invite people, change roles and remove members. They still can't touch billing or the account owner."
            : "Members can generate, draft and schedule content, but can't manage the team."
        }
      />

      <ConfirmDialog
        open={removing !== null}
        onClose={() => setRemoving(null)}
        onConfirm={() => void removeMember()}
        title="Remove this member"
        busy={busy}
        confirmLabel="Remove member"
        body={
          <>
            {shortId(removing?.user_id, 14)} loses access to this account immediately on
            their next token refresh. Content and credits they created stay with the
            account.
          </>
        }
      />
    </div>
  );
}

// ---- invite --------------------------------------------------------------

function InviteModal({
  open,
  accountId,
  isOwner,
  onClose,
  onInvited,
}: {
  open: boolean;
  accountId: string;
  isOwner: boolean;
  onClose: () => void;
  onInvited: (invite: InviteCreated) => void;
}) {
  const { toast } = useToast();
  const [email, setEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<AssignableRole>("member");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const invite = await userApi.invite(accountId, email.trim(), inviteRole);
      toast("success", "Invite sent", `${invite.email} was invited as ${invite.role}.`);
      setEmail("");
      setInviteRole("member");
      onInvited(invite);
    } catch (err) {
      toast("error", "Couldn't send invite", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Invite a member"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" form="invite-form" loading={busy}>
            Send invite
          </Button>
        </>
      }
    >
      <form id="invite-form" onSubmit={submit} className="space-y-4">
        <Field label="Email address" hint="They'll get a link that adds them to this account.">
          <Input
            type="email"
            required
            autoFocus
            placeholder="teammate@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field
          label="Role"
          hint={ROLE_OPTIONS.find((o) => o.value === inviteRole)?.hint}
        >
          <Select
            value={inviteRole}
            onChange={(e) => setInviteRole(e.target.value as AssignableRole)}
          >
            {ROLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value} disabled={option.value === "admin" && !isOwner}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
      </form>
    </Modal>
  );
}

/**
 * Dev affordance: while the User service runs with USER_EXPOSE_DEV_TOKENS=1
 * there is no mail delivery, so the raw invite token comes back in the
 * response. Surfacing it here is what makes the invite flow demonstrable
 * end-to-end; the modal simply doesn't appear once real email is wired up.
 */
function InviteSentModal({
  invite,
  onClose,
}: {
  invite: InviteCreated | null;
  onClose: () => void;
}) {
  const { toast } = useToast();
  const link = invite?.dev_invite_token
    ? `${window.location.origin}/onboarding?invite=${encodeURIComponent(invite.dev_invite_token)}`
    : null;

  return (
    <Modal
      open={invite !== null && link !== null}
      onClose={onClose}
      title="Invite created"
      footer={
        <Button onClick={onClose}>Done</Button>
      }
    >
      <div className="space-y-3">
        <p className="text-sm leading-relaxed text-ink-soft">
          Email delivery isn't wired up in this environment, so here's the join link for{" "}
          <span className="text-ink">{invite?.email}</span>. It expires{" "}
          {invite ? formatDateTime(invite.expires_at) : ""}.
        </p>
        <div className="flex items-center gap-2 rounded-field border border-edge-strong bg-surface-2 p-2">
          <code className="min-w-0 flex-1 truncate text-2xs text-ink-soft">{link}</code>
          <Button
            variant="secondary"
            size="sm"
            icon={<Copy size={13} />}
            onClick={() => {
              if (link) void navigator.clipboard.writeText(link);
              toast("success", "Link copied");
            }}
          >
            Copy
          </Button>
        </div>
        <p className="flex items-start gap-2 text-xs leading-relaxed text-ink-faint">
          <Shield size={13} className="mt-0.5 shrink-0" />
          The invitee must be signed in when they open it — accepting an invite is what
          links their user to this account.
        </p>
      </div>
    </Modal>
  );
}
