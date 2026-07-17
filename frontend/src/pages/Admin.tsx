import { useState } from "react";
import { Navigate } from "react-router-dom";
import {
  Activity,
  Building2,
  KeyRound,
  ScrollText,
  Search,
  ShieldCheck,
  Users,
} from "lucide-react";
import { Badge, StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { EmptyState, ErrorState } from "../components/ui/EmptyState";
import { Input } from "../components/ui/Input";
import { ConfirmDialog } from "../components/ui/Modal";
import { PageHeader } from "../components/ui/PageHeader";
import { StatCard } from "../components/ui/StatCard";
import { Table, type Column } from "../components/ui/Table";
import { Tabs } from "../components/ui/Tabs";
import { ADMIN_ROLES, useAuth } from "../hooks/useAuth";
import { useApi } from "../hooks/useApi";
import { useToast } from "../hooks/useToast";
import { ApiError } from "../lib/api/client";
import { adminApi } from "../lib/api/endpoints";
import type { AdminAccount, AdminSession, AdminUser, AuditEntry } from "../lib/api/types";
import { formatDateTime, formatNumber, formatRelative, shortId } from "../lib/format";

export default function Admin() {
  const { role, ready } = useAuth();
  const [tab, setTab] = useState("audit");

  if (ready && (role === null || !ADMIN_ROLES.includes(role))) {
    // Route guard: non-admins are redirected, not just hidden (spec §4).
    return <Navigate to="/dashboard" replace />;
  }

  return (
    <div className="animate-fade-up">
      <PageHeader
        title="Admin console"
        subtitle={
          role === "superadmin"
            ? "Platform-wide view — every account, session, and audit event."
            : "Scoped to your account's activity and sessions."
        }
      />

      <StatsRow />

      <div className="mt-6">
        <Tabs
          tabs={[
            { id: "audit", label: "Audit log" },
            { id: "sessions", label: "Active sessions" },
            { id: "accounts", label: "Accounts" },
            { id: "users", label: "Users" },
          ]}
          active={tab}
          onChange={setTab}
        />
        <div className="mt-4">
          {tab === "audit" && <AuditLog />}
          {tab === "sessions" && <Sessions />}
          {tab === "accounts" && <Accounts />}
          {tab === "users" && <UsersDirectory />}
        </div>
      </div>
    </div>
  );
}

// ---- stats ---------------------------------------------------------------

function StatsRow() {
  const stats = useApi(() => adminApi.stats(), []);
  // /admin/stats is superadmin-only — tenant admins just skip the row.
  if (stats.error) return null;
  return (
    <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
      <StatCard
        label="Accounts"
        value={stats.data ? formatNumber(stats.data.accounts.total) : "—"}
        sub={stats.data ? `${stats.data.accounts.suspended} suspended` : undefined}
        icon={<Building2 size={15} />}
        loading={stats.loading}
      />
      <StatCard
        label="Users"
        value={stats.data ? formatNumber(stats.data.users.total) : "—"}
        sub={stats.data ? `${stats.data.users.suspended} suspended` : undefined}
        icon={<Users size={15} />}
        loading={stats.loading}
      />
      <StatCard
        label="Audit events"
        value={stats.data ? formatNumber(stats.data.audit_events) : "—"}
        icon={<ScrollText size={15} />}
        loading={stats.loading}
      />
      <StatCard
        label="Live sessions"
        value={stats.data ? formatNumber(stats.data.active_sessions) : "—"}
        sub="from Redis"
        icon={<Activity size={15} />}
        loading={stats.loading}
      />
    </div>
  );
}

// ---- audit log -----------------------------------------------------------

function AuditLog() {
  const [routingKey, setRoutingKey] = useState("");
  const [accountId, setAccountId] = useState("");
  const [applied, setApplied] = useState({ routingKey: "", accountId: "" });

  const log = useApi(
    () =>
      adminApi.auditLog({
        routing_key: applied.routingKey || undefined,
        account_id: applied.accountId || undefined,
        limit: 50,
      }),
    [applied],
  );

  const columns: Column<AuditEntry>[] = [
    {
      key: "when",
      header: "When",
      render: (e) => <span className="whitespace-nowrap">{formatDateTime(e.created_at)}</span>,
    },
    {
      key: "event",
      header: "Event",
      render: (e) => <Badge tone="accent">{e.routing_key ?? "—"}</Badge>,
    },
    {
      key: "account",
      header: "Account",
      render: (e) => <span className="font-mono text-xs">{shortId(e.account_id)}</span>,
    },
    {
      key: "actor",
      header: "Actor",
      render: (e) => <span className="font-mono text-xs">{shortId(e.actor_user_id)}</span>,
    },
    {
      key: "payload",
      header: "Payload",
      render: (e) => (
        <span className="block max-w-[18rem] truncate font-mono text-2xs text-ink-faint">
          {JSON.stringify(e.payload)}
        </span>
      ),
    },
  ];

  return (
    <>
      <form
        className="mb-3 flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied({ routingKey: routingKey.trim(), accountId: accountId.trim() });
        }}
      >
        <Input
          placeholder="Routing key (e.g. credits.debited)"
          value={routingKey}
          onChange={(e) => setRoutingKey(e.target.value)}
          className="max-w-56"
        />
        <Input
          placeholder="Account ID"
          value={accountId}
          onChange={(e) => setAccountId(e.target.value)}
          className="max-w-56"
        />
        <Button type="submit" variant="secondary" size="sm" icon={<Search size={13} />}>
          Filter
        </Button>
      </form>
      <Card padded={false} className="p-2">
        {log.error ? (
          <ErrorState error={log.error} onRetry={log.reload} />
        ) : (
          <Table
            columns={columns}
            rows={log.data?.items ?? []}
            rowKey={(e) => e.audit_id}
            loading={log.loading}
            empty={
              <EmptyState
                icon={<ScrollText size={18} />}
                title="No audit events"
                body="Domain events (signups, credit movements, publishes) are recorded here as they happen."
              />
            }
          />
        )}
      </Card>
    </>
  );
}

// ---- sessions ------------------------------------------------------------

function Sessions() {
  const { toast } = useToast();
  const { claims } = useAuth();
  const sessions = useApi(() => adminApi.sessions(), []);
  const [revoking, setRevoking] = useState<AdminSession | null>(null);
  const [busy, setBusy] = useState(false);

  async function revoke() {
    if (!revoking) return;
    setBusy(true);
    try {
      await adminApi.revokeSession(revoking.jti);
      toast("success", "Session revoked", "The token is invalid immediately, platform-wide.");
      setRevoking(null);
      sessions.reload();
    } catch (err) {
      toast("error", "Revoke failed", err instanceof ApiError ? err.message : undefined);
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<AdminSession>[] = [
    {
      key: "jti",
      header: "Session (jti)",
      render: (s) => (
        <span className="font-mono text-xs">
          {shortId(s.jti, 14)}
          {s.jti === claims?.jti && (
            <Badge tone="accent" className="ml-2">
              this session
            </Badge>
          )}
        </span>
      ),
    },
    { key: "user", header: "User", render: (s) => <span className="font-mono text-xs">{shortId(s.user_id)}</span> },
    { key: "account", header: "Account", render: (s) => <span className="font-mono text-xs">{shortId(s.account_id)}</span> },
    { key: "role", header: "Role", render: (s) => <Badge tone="neutral" className="capitalize">{s.role}</Badge> },
    {
      key: "revoke",
      header: "",
      align: "right",
      render: (s) => (
        <Button variant="danger" size="sm" icon={<KeyRound size={12} />} onClick={() => setRevoking(s)}>
          Revoke
        </Button>
      ),
    },
  ];

  return (
    <>
      <Card padded={false} className="p-2">
        {sessions.error ? (
          <ErrorState error={sessions.error} onRetry={sessions.reload} />
        ) : (
          <Table
            columns={columns}
            rows={sessions.data?.items ?? []}
            rowKey={(s) => s.jti}
            loading={sessions.loading}
            empty={
              <EmptyState
                icon={<KeyRound size={18} />}
                title="No live sessions"
                body="Active JWT sessions are read live from Redis."
              />
            }
          />
        )}
      </Card>
      <ConfirmDialog
        open={revoking !== null}
        onClose={() => setRevoking(null)}
        onConfirm={() => void revoke()}
        title="Revoke session"
        busy={busy}
        confirmLabel="Revoke"
        body={
          revoking?.jti === claims?.jti
            ? "This is YOUR current session — revoking it signs you out immediately."
            : `Force-logout session ${shortId(revoking?.jti, 14)}? The user must sign in again.`
        }
      />
    </>
  );
}

// ---- accounts ------------------------------------------------------------

function Accounts() {
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const accounts = useApi(() => adminApi.accounts({ q: applied || undefined, limit: 50 }), [applied]);

  const columns: Column<AdminAccount>[] = [
    {
      key: "name",
      header: "Account",
      render: (a) => (
        <div>
          <p className="font-medium text-ink">{a.name ?? shortId(a.account_id)}</p>
          <p className="font-mono text-2xs text-ink-faint">{a.account_id}</p>
        </div>
      ),
    },
    { key: "type", header: "Type", render: (a) => <Badge tone="neutral" className="capitalize">{a.type ?? "—"}</Badge> },
    { key: "plan", header: "Plan", render: (a) => <span className="capitalize">{a.plan_tier ?? "—"}</span> },
    { key: "status", header: "Status", render: (a) => <StatusBadge status={a.status} /> },
    {
      key: "seen",
      header: "First seen",
      render: (a) => <span className="whitespace-nowrap">{formatRelative(a.first_seen_at)}</span>,
    },
  ];

  return (
    <>
      <form
        className="mb-3 flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied(q.trim());
        }}
      >
        <Input
          placeholder="Search by name or ID"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="max-w-72"
        />
        <Button type="submit" variant="secondary" size="sm" icon={<Search size={13} />}>
          Search
        </Button>
      </form>
      <Card padded={false} className="p-2">
        {accounts.error ? (
          <ErrorState error={accounts.error} onRetry={accounts.reload} />
        ) : (
          <Table
            columns={columns}
            rows={accounts.data?.items ?? []}
            rowKey={(a) => a.account_id}
            loading={accounts.loading}
            empty={
              <EmptyState
                icon={<Building2 size={18} />}
                title="No accounts found"
                body="The directory fills as accounts register on the platform."
              />
            }
          />
        )}
      </Card>
    </>
  );
}

// ---- users ---------------------------------------------------------------

function UsersDirectory() {
  const { role } = useAuth();
  const users = useApi(() => adminApi.users({ limit: 50 }), []);

  if (role !== "superadmin" || users.error?.status === 403) {
    return (
      <Card>
        <EmptyState
          icon={<ShieldCheck size={18} />}
          title="SuperAdmin only"
          body="The cross-account user directory needs the platform superadmin role."
        />
      </Card>
    );
  }

  const columns: Column<AdminUser>[] = [
    {
      key: "email",
      header: "User",
      render: (u) => (
        <div>
          <p className="font-medium text-ink">{u.email ?? "—"}</p>
          <p className="font-mono text-2xs text-ink-faint">{u.user_id}</p>
        </div>
      ),
    },
    { key: "status", header: "Status", render: (u) => <StatusBadge status={u.status} /> },
    {
      key: "seen",
      header: "First seen",
      render: (u) => <span className="whitespace-nowrap">{formatRelative(u.first_seen_at)}</span>,
    },
    {
      key: "reason",
      header: "Suspend reason",
      render: (u) => <span className="text-ink-faint">{u.suspend_reason ?? "—"}</span>,
    },
  ];

  return (
    <Card padded={false} className="p-2">
      {users.error ? (
        <ErrorState error={users.error} onRetry={users.reload} />
      ) : (
        <Table
          columns={columns}
          rows={users.data?.items ?? []}
          rowKey={(u) => u.user_id}
          loading={users.loading}
          empty={
            <EmptyState
              icon={<Users size={18} />}
              title="No users found"
              body="Registered users appear here as the platform grows."
            />
          }
        />
      )}
    </Card>
  );
}
