/**
 * API response shapes, transcribed from each backend service's route
 * handlers (services/<name>/routes.py) — the services return plain dicts,
 * so these interfaces are the frontend's contract with the gateway.
 */

// ---- auth ----------------------------------------------------------------

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface SignupResponse {
  user_id: string;
  email: string;
  message: string;
  /** Present only while AUTH_EXPOSE_DEV_TOKENS=1 (no mail delivery yet). */
  dev_verification_token?: string;
}

export interface MeResponse {
  user_id: string;
  account_id: string;
  role: Role;
  jti: string;
}

export type Role = "owner" | "admin" | "member" | "superadmin";

// ---- user / accounts -----------------------------------------------------

export interface AccountSummary {
  account_id: string;
  type: "individual" | "team";
  name: string | null;
  plan_tier: string;
  role: Role;
}

export interface MyAccountsResponse {
  user_id: string;
  accounts: AccountSummary[];
}

/** Full profile from GET/POST/PATCH /accounts/{id} — adds the seat count the
 *  dashboard header and team screen show. */
export interface AccountProfile {
  account_id: string;
  type: "individual" | "team";
  name: string | null;
  plan_tier: string;
  seat_count: number;
  created_at: string;
}

/** Roles assignable to a member. "owner" is fixed at account creation and
 *  cannot be granted (services/user/routes.py), so it is not in this union. */
export type AssignableRole = "admin" | "member";

export interface AccountMember {
  user_id: string;
  role: Role;
  joined_at: string;
}

export interface MembersResponse {
  account_id: string;
  members: AccountMember[];
}

export interface InviteCreated {
  invite_id: string;
  email: string;
  role: Role;
  expires_at: string;
  message: string;
  /** Present only while USER_EXPOSE_DEV_TOKENS=1 (no mail delivery yet) — the
   *  team screen surfaces it so an invite can be tested without email. */
  dev_invite_token?: string;
}

export interface InviteAccepted {
  account_id: string;
  account_name: string | null;
  role: Role;
  message: string;
}

// ---- credits -------------------------------------------------------------

export interface CreditBalance {
  account_id: string;
  balance: number;
  low_balance_threshold: number;
}

export interface LedgerEntry {
  id: string;
  amount: number;
  entry_type: string;
  stripe_ref: string | null;
  listing_id: string | null;
  counterparty_account_id: string | null;
  money_amount_cents: number | null;
  reason: string | null;
  created_by_user_id: string | null;
  created_at: string;
}

export interface CreditHistory {
  account_id: string;
  balance: number;
  entries: LedgerEntry[];
}

export interface MarketplaceListing {
  listing_id: string;
  seller_account_id: string;
  credits_amount: number;
  price_cents: number;
  status: "open" | "sold" | "canceled";
  buyer_account_id: string | null;
  created_at: string;
  sold_at: string | null;
}

// ---- usage ---------------------------------------------------------------

export interface UsageByModel {
  model: string;
  generations: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export interface UsageSummary {
  account_id: string;
  period: string; // YYYY-MM
  quota_tokens: number;
  used_tokens: number;
  used_percent: number | null;
  total_generations: number;
  total_cost_usd: number;
  by_model: UsageByModel[];
}

// ---- billing -------------------------------------------------------------

export interface Subscription {
  account_id: string;
  plan: "free" | "pro" | "team";
  status: string;
  stripe_customer_id: string | null;
  grace_expires_at: string | null;
}

export interface Invoice {
  invoice_id: string;
  stripe_invoice_id: string | null;
  status: string;
  amount_due: number;
  amount_paid: number;
  currency: string;
  hosted_invoice_url: string | null;
  created_at: string;
}

export interface CheckoutSession {
  checkout_url: string;
  checkout_session_id: string;
  plan: string;
}

// ---- ai generation -------------------------------------------------------

export interface ModelOption {
  alias: string;
  model: string;
}

export interface GenerationAccepted {
  job_id: string;
  status: string;
  model: string;
  stream_url: string;
}

export type GenerationStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface GenerationJob {
  job_id: string;
  account_id: string;
  model: string;
  status: GenerationStatus;
  prompt: string;
  response: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cost_usd: number | null;
  usage_estimated: boolean | null;
  error_reason: string | null;
  created_at: string | null;
  completed_at: string | null;
  stream_url: string;
}

/** One SSE frame from GET /generations/{id}/stream. */
export interface StreamMessage {
  seq: number;
  type: "token" | "done" | "cancelled" | "error";
  content?: string;
  reason?: string;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
}

// ---- content -------------------------------------------------------------

export type ContentStatus = "draft" | "approved" | "scheduled" | "published";

export interface ContentItem {
  content_id: string;
  account_id: string;
  created_by_user_id: string | null;
  generation_job_id: string | null;
  title: string;
  body: string;
  tags: string[];
  status: ContentStatus;
  version: number;
  image_url: string | null;
  image_asset_ref: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ---- scheduler -----------------------------------------------------------

export type ScheduleStatus = "pending" | "fired" | "cancelled";

export interface Schedule {
  schedule_id: string;
  account_id: string;
  content_id: string;
  created_by_user_id: string | null;
  publish_at: string;
  timezone: string;
  recurrence: string | null;
  status: ScheduleStatus;
  fire_count: number;
  last_fired_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CalendarResponse {
  start: string;
  end: string;
  items: Schedule[];
}

// ---- social --------------------------------------------------------------

export interface SocialConnection {
  connection_id: string;
  account_id: string;
  provider: string;
  member_urn: string | null;
  display_name: string | null;
  status: "connected" | "disconnected";
  scope: string | null;
  token_expires_at: string | null;
  connected_by_user_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface OAuthStart {
  authorization_url: string;
  state: string;
  expires_in: number;
}

export interface PublishJob {
  job_id: string;
  account_id: string;
  content_id: string;
  schedule_id: string | null;
  connection_id: string | null;
  source: "manual" | "scheduled";
  status: "published" | "failed";
  text: string | null;
  text_source: string | null;
  image_included: boolean;
  linkedin_post_id: string | null;
  linkedin_post_url: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ---- scraper -------------------------------------------------------------

export interface ScrapeJob {
  job_id: string;
  account_id: string;
  url: string;
  job_type: string;
  recurrence: string | null;
  status: string;
  source: string | null;
  run_count: number;
  result_document_id: string | null;
  error: string | null;
  request_event_id: string | null;
  created_by_user_id: string | null;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ScrapedDocument {
  document_id: string;
  account_id: string;
  job_id: string;
  url: string | null;
  final_url: string | null;
  job_type: string | null;
  title: string | null;
  description: string | null;
  status_code: number | null;
  scraped_at: string | null;
  headings?: string[] | null;
  text?: string | null;
  links?: { href: string; text: string }[] | string[] | null;
  html?: string | null;
}

// ---- notifications -------------------------------------------------------

export interface NotificationLog {
  notification_id: string;
  event_id: string | null;
  routing_key: string | null;
  template: string | null;
  account_id: string | null;
  recipient: string | null;
  subject: string | null;
  provider: string | null;
  provider_message_id: string | null;
  status: "sent" | "failed";
  error: string | null;
  created_at: string | null;
}

// ---- admin ---------------------------------------------------------------

export interface AuditEntry {
  audit_id: string;
  event_id: string | null;
  exchange: string | null;
  routing_key: string | null;
  account_id: string | null;
  actor_user_id: string | null;
  payload: Record<string, unknown>;
  created_at: string | null;
}

export interface AdminSession {
  jti: string;
  user_id: string;
  account_id: string;
  role: string;
  [key: string]: unknown;
}

export interface AdminAccount {
  account_id: string;
  name: string | null;
  type: string | null;
  plan_tier: string | null;
  owner_user_id: string | null;
  status: "active" | "suspended";
  suspended_at: string | null;
  suspended_by: string | null;
  suspend_reason: string | null;
  first_seen_at: string | null;
}

export interface AdminUser {
  user_id: string;
  email: string | null;
  status: "active" | "suspended";
  suspended_at: string | null;
  suspended_by: string | null;
  suspend_reason: string | null;
  first_seen_at: string | null;
}

export interface PlatformStats {
  accounts: { total: number; suspended: number };
  users: { total: number; suspended: number };
  audit_events: number;
  active_sessions: number;
}

// ---- shared --------------------------------------------------------------

export interface Paginated<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
