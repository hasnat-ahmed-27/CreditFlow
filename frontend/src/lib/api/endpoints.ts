/**
 * Typed endpoint functions, grouped by owning service. All paths are public
 * gateway paths (services declare full paths; the gateway routes on the
 * first segment).
 */
import { api } from "./client";
import type {
  AdminAccount,
  AdminSession,
  AdminUser,
  AuditEntry,
  CalendarResponse,
  CheckoutSession,
  ContentItem,
  ContentStatus,
  CreditBalance,
  CreditHistory,
  GenerationAccepted,
  GenerationJob,
  Invoice,
  MarketplaceListing,
  MeResponse,
  ModelOption,
  MyAccountsResponse,
  NotificationLog,
  OAuthStart,
  Paginated,
  PlatformStats,
  PublishJob,
  Schedule,
  ScrapeJob,
  ScrapedDocument,
  SignupResponse,
  SocialConnection,
  Subscription,
  TokenPair,
  UsageSummary,
} from "./types";

// ---- auth ----------------------------------------------------------------

export const authApi = {
  signup: (email: string, password: string) =>
    api.post<SignupResponse>("/auth/signup", { email, password }),
  verifyEmail: (token: string) =>
    api.post<{ message: string }>("/auth/verify-email", { token }),
  login: (email: string, password: string) =>
    api.post<TokenPair>("/auth/login", { email, password }),
  logout: (refresh_token: string | null) =>
    api.post<{ message: string }>("/auth/logout", refresh_token ? { refresh_token } : undefined),
  requestPasswordReset: (email: string) =>
    api.post<{ message: string; dev_reset_token?: string }>(
      "/auth/password-reset/request",
      { email },
    ),
  confirmPasswordReset: (token: string, new_password: string) =>
    api.post<{ message: string }>("/auth/password-reset/confirm", { token, new_password }),
  me: () => api.get<MeResponse>("/me"),
};

// ---- user / accounts -----------------------------------------------------

export const userApi = {
  myAccounts: () => api.get<MyAccountsResponse>("/users/me/accounts"),
};

// ---- credits -------------------------------------------------------------

export const creditsApi = {
  balance: () => api.get<CreditBalance>("/credits/balance"),
  history: (limit = 200) => api.get<CreditHistory>("/credits/history", { limit }),
  listings: () => api.get<{ listings: MarketplaceListing[] }>("/credits/marketplace/listings"),
  createListing: (credits_amount: number, price_cents: number) =>
    api.post<MarketplaceListing>("/credits/marketplace/listings", { credits_amount, price_cents }),
  cancelListing: (listingId: string) =>
    api.delete<MarketplaceListing>(`/credits/marketplace/listings/${listingId}`),
  purchaseListing: (listingId: string) =>
    api.post<MarketplaceListing>(`/credits/marketplace/listings/${listingId}/purchase`),
};

// ---- usage ---------------------------------------------------------------

export const usageApi = {
  summary: (period?: string) => api.get<UsageSummary>("/usage/summary", { period }),
};

// ---- billing -------------------------------------------------------------

export const billingApi = {
  subscription: () => api.get<Subscription>("/billing/subscription"),
  checkout: (plan: "pro" | "team") => api.post<CheckoutSession>("/billing/checkout", { plan }),
  changePlan: (plan: "free" | "pro" | "team") =>
    api.post<Subscription>("/billing/plan", { plan }),
  invoices: () => api.get<{ account_id: string; invoices: Invoice[] }>("/billing/invoices"),
};

// ---- ai ------------------------------------------------------------------

export const aiApi = {
  models: () => api.get<{ models: ModelOption[] }>("/models"),
  create: (prompt: string, model: string, max_tokens?: number) =>
    api.post<GenerationAccepted>("/generations", { prompt, model, max_tokens }),
  get: (jobId: string) => api.get<GenerationJob>(`/generations/${jobId}`),
  cancel: (jobId: string) =>
    api.post<{ job_id: string; status: string }>(`/generations/${jobId}/cancel`),
};

// ---- content -------------------------------------------------------------

export interface ContentDraft {
  title: string;
  body: string;
  tags?: string[];
  image_url?: string | null;
  generation_job_id?: string | null;
}

export const contentApi = {
  list: (params: { status?: string; tag?: string; limit?: number; offset?: number } = {}) =>
    api.get<Paginated<ContentItem>>("/content", params),
  get: (contentId: string) => api.get<ContentItem>(`/content/${contentId}`),
  create: (draft: ContentDraft) => api.post<ContentItem>("/content", draft),
  update: (contentId: string, patch: Partial<ContentDraft>) =>
    api.patch<ContentItem>(`/content/${contentId}`, patch),
  remove: (contentId: string) => api.delete<null>(`/content/${contentId}`),
  setStatus: (contentId: string, status: ContentStatus) =>
    api.post<ContentItem>(`/content/${contentId}/status`, { status }),
};

// ---- scheduler -----------------------------------------------------------

export const schedulerApi = {
  list: (params: { status?: string; limit?: number; offset?: number } = {}) =>
    api.get<Paginated<Schedule>>("/schedules", params),
  calendar: (start: string, end: string, status?: string) =>
    api.get<CalendarResponse>("/schedules/calendar", { start, end, status }),
  create: (body: {
    content_id: string;
    publish_at: string;
    timezone?: string;
    recurrence?: string | null;
  }) => api.post<Schedule>("/schedules", body),
  update: (
    scheduleId: string,
    patch: { publish_at?: string; timezone?: string; recurrence?: string | null },
  ) => api.patch<Schedule>(`/schedules/${scheduleId}`, patch),
  cancel: (scheduleId: string) => api.delete<Schedule>(`/schedules/${scheduleId}`),
};

// ---- social --------------------------------------------------------------

export const socialApi = {
  connections: () => api.get<{ items: SocialConnection[] }>("/connections"),
  startLinkedIn: () => api.post<OAuthStart>("/connections/linkedin/start"),
  finishLinkedIn: (code: string, state: string) =>
    api.post<SocialConnection>("/connections/linkedin/callback", { code, state }),
  disconnect: (connectionId: string) =>
    api.delete<SocialConnection>(`/connections/${connectionId}`),
  publish: (content_id: string) => api.post<PublishJob>("/publish", { content_id }),
  publishJobs: (params: { status?: string; content_id?: string; limit?: number; offset?: number } = {}) =>
    api.get<Paginated<PublishJob>>("/publish-jobs", params),
};

// ---- scraper -------------------------------------------------------------

export const scraperApi = {
  create: (url: string, job_type = "page", recurrence?: string | null) =>
    api.post<ScrapeJob>("/scrape-jobs", { url, job_type, recurrence }),
  list: (params: { status?: string; limit?: number; offset?: number } = {}) =>
    api.get<Paginated<ScrapeJob>>("/scrape-jobs", params),
  get: (jobId: string) =>
    api.get<ScrapeJob & { document: ScrapedDocument | null }>(`/scrape-jobs/${jobId}`),
  documents: (jobId: string) =>
    api.get<Paginated<ScrapedDocument>>(`/scrape-jobs/${jobId}/documents`),
  remove: (jobId: string) =>
    api.delete<{ deleted: boolean; job_id: string }>(`/scrape-jobs/${jobId}`),
  document: (documentId: string) =>
    api.get<ScrapedDocument>(`/scraped-documents/${documentId}`),
};

// ---- notifications -------------------------------------------------------

export const notificationsApi = {
  list: (params: { status?: string; limit?: number; offset?: number } = {}) =>
    api.get<Paginated<NotificationLog>>("/notifications", params),
};

// ---- admin ---------------------------------------------------------------

export const adminApi = {
  auditLog: (params: {
    account_id?: string;
    actor_user_id?: string;
    routing_key?: string;
    since?: string;
    until?: string;
    limit?: number;
    offset?: number;
  } = {}) => api.get<Paginated<AuditEntry>>("/admin/audit-log", params),
  sessions: (params: { account_id?: string; user_id?: string } = {}) =>
    api.get<{ total: number; items: AdminSession[] }>("/admin/sessions", params),
  revokeSession: (jti: string) =>
    api.delete<{ jti: string; revoked: boolean }>(`/admin/sessions/${jti}`),
  accounts: (params: { q?: string; status?: string; limit?: number; offset?: number } = {}) =>
    api.get<Paginated<AdminAccount>>("/admin/accounts", params),
  users: (params: { q?: string; status?: string; limit?: number; offset?: number } = {}) =>
    api.get<Paginated<AdminUser>>("/admin/users", params),
  stats: () => api.get<PlatformStats>("/admin/stats"),
};
