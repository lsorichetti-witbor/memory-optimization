"use client";

import { useState } from "react";
import { AlertTriangle, Check, Copy, RefreshCw, Trash2 } from "lucide-react";
import { format } from "date-fns";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import { toast } from "@/components/ui/use-toast";
import { api } from "@/utils/api";
import { PENDING_ENDPOINTS } from "@/utils/api-endpoints";
import { getErrorMessage } from "@/lib/error-message";
import { useApiQuery } from "@/hooks/use-api-query";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

interface PendingItem {
  id: string;
  state: "pending" | "embedding" | "error" | "dead";
  text: string;
  user_id: string | null;
  agent_id: string | null;
  run_id: string | null;
  attempts: number;
  last_error: string | null;
  last_error_code: string | null;
  next_attempt_at: string | null;
  claimed_by: string | null;
  lease_until: string | null;
  created_at: string | null;
  source_created_at: string | null;
  content_hash: string | null;
  idempotency_key: string | null;
}

interface CircuitBreaker {
  open: boolean;
  code: string | null;
  reason: string | null;
  retry_at: string | null;
  probing: boolean;
}

interface PendingResponse {
  total: number;
  not_searchable: number;
  pending: number;
  embedding: number;
  error: number;
  dead: number;
  oldest_queued_at: string | null;
  circuit_breaker: CircuitBreaker;
  items: PendingItem[];
}

const EMPTY: PendingResponse = {
  total: 0,
  not_searchable: 0,
  pending: 0,
  embedding: 0,
  error: 0,
  dead: 0,
  oldest_queued_at: null,
  circuit_breaker: { open: false, code: null, reason: null, retry_at: null, probing: false },
  items: [],
};

// pending and error are deliberately different things: nobody has tried yet,
// versus the provider refused. Only the second is re-armed when some other
// embedding succeeds, so showing them as one number would hide the distinction
// the queue is built around.
const STATE_STYLE: Record<PendingItem["state"], string> = {
  pending: "text-onSurface-default-secondary",
  embedding: "text-memGold-600 border-memGold-300",
  error: "text-onSurface-danger-primary border-onSurface-danger-primary",
  dead: "text-onSurface-danger-primary border-onSurface-danger-primary",
};

/** `embedding` means a worker holds it. An expired lease means nobody does.
 *
 * Both render as "embedding", and the second is the worse place to be stuck
 * because it looks like progress. The lease is the only evidence available - a
 * dead process does not announce itself - so a row past its lease is shown as
 * stalled and the claim query treats it as free.
 */
function isStalled(row: PendingItem): boolean {
  if (row.state !== "embedding") return false;
  if (!row.lease_until) return true;
  return new Date(row.lease_until).getTime() < Date.now();
}

/** Copy a value without leaving the page.
 *
 * A provider error is the one field here that has to leave the dashboard - it
 * goes into a bug report, a support thread, or a search. Selecting it by hand
 * out of a clipped table cell is where people give up and paraphrase, and a
 * paraphrased error is the one that cannot be looked up.
 */
function CopyValue({ value, label }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async (event: React.MouseEvent) => {
    // The row opens a detail panel; copying must not also open it.
    event.stopPropagation();
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast({
        title: "Could not copy",
        description: "The browser blocked clipboard access.",
        variant: "destructive",
      });
    }
  };

  return (
    <button
      type="button"
      onClick={copy}
      title={label ?? "Copy"}
      aria-label={copied ? "Copied" : (label ?? "Copy")}
      className="inline-flex items-center shrink-0 rounded p-1 hover:bg-surface-default-primary-hover"
    >
      {copied ? (
        <Check className="size-3.5 text-onSurface-success-primary" />
      ) : (
        <Copy className="size-3.5 text-onSurface-default-secondary" />
      )}
    </button>
  );
}

function Field({
  label,
  value,
  mono,
  copyable,
}: {
  label: string;
  value: string | null | undefined;
  mono?: boolean;
  copyable?: boolean;
}) {
  const shown = value ?? "-";
  return (
    <div className="space-y-1">
      <div className="text-xs uppercase text-onSurface-default-secondary">{label}</div>
      <div className="flex items-start gap-2">
        <div
          className={`flex-1 whitespace-pre-wrap break-words text-sm ${
            mono ? "font-mono text-xs" : ""
          }`}
        >
          {shown}
        </div>
        {copyable && value ? <CopyValue value={value} label={`Copy ${label}`} /> : null}
      </div>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: number; hint?: string }) {
  return (
    <Card className="border-memBorder-primary p-4">
      <div className="text-xs uppercase text-onSurface-default-secondary">{label}</div>
      <div className="text-2xl font-semibold font-fustat">{value}</div>
      {hint && <div className="text-xs text-onSurface-default-secondary mt-1">{hint}</div>}
    </Card>
  );
}

export default function QueuePage() {
  const [toDelete, setToDelete] = useState<PendingItem | null>(null);
  const [detail, setDetail] = useState<PendingItem | null>(null);
  const [retrying, setRetrying] = useState(false);

  const {
    data = EMPTY,
    isLoading,
    refetch,
  } = useApiQuery<PendingResponse>(
    async () => {
      const res = await api.get<PendingResponse>(PENDING_ENDPOINTS.BASE);
      return res.data ?? EMPTY;
    },
    { errorToast: "Failed to load the embedding queue", initialData: EMPTY },
  );

  const handleRetry = async (force = false) => {
    setRetrying(true);
    try {
      const res = await api.post<{ note?: string; attempting?: boolean }>(
        PENDING_ENDPOINTS.RETRY(force),
        {},
      );
      // The server decides whether anything is actually attempted, so it says
      // so. Reporting a flat "re-armed" beside a parked banner left it unclear
      // whether the provider had been called.
      toast({
        title: res.data?.attempting ? "Re-armed, worker running" : "Re-armed, queue still parked",
        description: res.data?.note,
        variant: "success",
      });
      void refetch();
    } catch (error) {
      toast({
        title: "Failed to re-arm the queue",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    } finally {
      setRetrying(false);
    }
  };

  const handleDelete = async () => {
    if (!toDelete) return;
    try {
      await api.delete(PENDING_ENDPOINTS.BY_ID(toDelete.id));
      toast({ title: "Queued memory discarded", variant: "success" });
      setToDelete(null);
      void refetch();
    } catch (error) {
      toast({
        title: "Failed to discard",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  const columns = [
    {
      key: "state" as keyof PendingItem,
      label: "State",
      width: 90,
      render: (value: PendingItem["state"], row: PendingItem) => (
        <Badge
          variant="outline"
          className={`capitalize ${
            isStalled(row) ? STATE_STYLE.error : STATE_STYLE[value]
          }`}
          title={
            isStalled(row)
              ? "The worker that claimed this is gone. It will be picked up again."
              : undefined
          }
        >
          {isStalled(row) ? "stalled" : value}
        </Badge>
      ),
    },
    {
      key: "text" as keyof PendingItem,
      label: "Memory",
      width: 340,
      render: (value: string) => (
        <span className="line-clamp-2 text-onSurface-default-primary">{value}</span>
      ),
    },
    {
      key: "agent_id" as keyof PendingItem,
      label: "Scope",
      width: 180,
      render: (value: string | null, row: PendingItem) => (
        <span className="text-onSurface-default-secondary">
          {value ?? row.run_id ?? row.user_id ?? "-"}
        </span>
      ),
    },
    {
      key: "attempts" as keyof PendingItem,
      label: "Attempts",
      width: 80,
      render: (value: number) => <span>{value}</span>,
    },
    {
      key: "last_error_code" as keyof PendingItem,
      label: "Last error",
      width: 220,
      render: (value: string | null, row: PendingItem) => (
        <div className="flex items-center gap-1">
          <span
            className="text-onSurface-default-secondary line-clamp-2"
            title={row.last_error ?? undefined}
          >
            {value ?? "-"}
          </span>
          {row.last_error ? (
            <CopyValue value={row.last_error} label="Copy the full error" />
          ) : null}
        </div>
      ),
    },
    {
      key: "created_at" as keyof PendingItem,
      label: "Queued",
      width: 150,
      render: (value: string | null) =>
        value ? format(new Date(value), "yyyy-MM-dd HH:mm:ss") : "-",
    },
    {
      key: "id" as keyof PendingItem,
      label: "",
      width: 50,
      render: (_value: string, row: PendingItem) => (
        <Button variant="ghost" size="icon" onClick={() => setToDelete(row)} className="size-7">
          <Trash2 className="size-3.5 text-onSurface-danger-primary" />
        </Button>
      ),
    },
  ];

  const breaker = data.circuit_breaker;
  const breakerOpen = breaker.open;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold font-fustat">Embedding queue</h1>
          <p className="text-sm text-onSurface-default-secondary mt-1">
            Memories the server accepted but has not embedded yet.{" "}
            <strong>They are stored and will not be lost — but they are not searchable
            until they drain.</strong>
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            onClick={() => handleRetry(false)}
            disabled={retrying}
            variant={breakerOpen ? "outline" : "default"}
            className="gap-2"
            title={
              breakerOpen
                ? "Re-arms the rows. Nothing is sent to the provider while the queue is parked."
                : "Re-arm every error and dead row, and run the worker now."
            }
          >
            <RefreshCw className={`size-4 ${retrying ? "animate-spin" : ""}`} />
            {breakerOpen ? "Re-arm rows" : "Retry all now"}
          </Button>
          {breakerOpen && (
            <Button
              onClick={() => handleRetry(true)}
              disabled={retrying}
              className="gap-2"
              title="Only if the cause is actually fixed - a raised quota, a replaced key."
            >
              Probe now
            </Button>
          )}
        </div>
      </div>

      {breaker.open && (
        <Card className="border-onSurface-danger-primary p-4 flex gap-3 items-start">
          <AlertTriangle className="size-5 text-onSurface-danger-primary shrink-0 mt-0.5" />
          <div className="space-y-1">
            <div className="font-semibold">
              Queue parked — {breaker.code ?? "provider unavailable"}
            </div>
            <div className="text-sm text-onSurface-default-secondary">{breaker.reason}</div>
            <div className="text-xs text-onSurface-default-secondary">
              {breaker.probing
                ? "Probing now with a single request."
                : breaker.retry_at
                  ? `Next probe ${format(new Date(breaker.retry_at), "HH:mm:ss")}. Retrying every row
                     independently would spend the whole allowance discovering the same outage.`
                  : null}
            </div>
            <div className="text-xs text-onSurface-default-secondary">
              While parked, <strong>Re-arm rows</strong> only resets their state — nothing is sent
              to the provider. Use <strong>Probe now</strong> once the cause is actually fixed.
            </div>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Stat label="Not searchable" value={data.not_searchable} hint="everything below" />
        <Stat label="Pending" value={data.pending} hint="waiting for a worker" />
        <Stat label="Embedding" value={data.embedding} hint="claimed right now" />
        <Stat label="Error" value={data.error} hint="re-armed on next success" />
        <Stat label="Dead" value={data.dead} hint="needs a person" />
      </div>

      {isLoading ? (
        <TableSkeleton rows={5} columns={7} />
      ) : data.items.length === 0 ? (
        <EmptyState
          title="Nothing queued"
          description="Every memory that was accepted has been embedded and is searchable."
        />
      ) : (
        <Card className="border-memBorder-primary overflow-hidden">
          <DataTable
            data={data.items}
            columns={columns}
            getRowKey={(row) => row.id}
            onRowClick={(row) => setDetail(row)}
          />
        </Card>
      )}

      <Sheet open={!!detail} onOpenChange={(open) => !open && setDetail(null)}>
        <SheetContent className="w-full sm:max-w-xl overflow-y-auto">
          <SheetHeader>
            <SheetTitle className="flex items-center gap-2">
              Queued memory
              {detail && (
                <Badge
                  variant="outline"
                  className={`capitalize ${
                    isStalled(detail) ? STATE_STYLE.error : STATE_STYLE[detail.state]
                  }`}
                >
                  {isStalled(detail) ? "stalled" : detail.state}
                </Badge>
              )}
            </SheetTitle>
            <SheetDescription>
              Stored on the server and safe. Not searchable until it is embedded.
            </SheetDescription>
          </SheetHeader>

          {detail && (
            <div className="mt-6 space-y-5">
              <Field label="Memory" value={detail.text} copyable />

              {detail.last_error ? (
                <div className="space-y-1">
                  <div className="text-xs uppercase text-onSurface-default-secondary">
                    Last error
                  </div>
                  <div className="flex items-start gap-2">
                    <pre className="flex-1 whitespace-pre-wrap break-words rounded border border-memBorder-primary bg-surface-default-fg-secondary p-3 font-mono text-xs">
                      {detail.last_error}
                    </pre>
                    <CopyValue value={detail.last_error} label="Copy the full error" />
                  </div>
                  <div className="text-xs text-onSurface-default-secondary">
                    code: {detail.last_error_code ?? "-"} · attempt {detail.attempts}
                  </div>
                </div>
              ) : (
                <Field label="Last error" value={null} />
              )}

              {isStalled(detail) && (
                <Card className="border-onSurface-danger-primary p-3 text-sm">
                  The worker that claimed this row is gone — its lease has expired. No
                  embedding is in progress. The next worker pass will pick it up.
                </Card>
              )}

              <div className="grid grid-cols-2 gap-4">
                <Field label="Written at" value={detail.source_created_at} />
                <Field label="Queued at" value={detail.created_at} />
                <Field label="Next attempt" value={detail.next_attempt_at} />
                <Field label="Lease until" value={detail.lease_until} />
                <Field label="user_id" value={detail.user_id} />
                <Field label="agent_id" value={detail.agent_id} />
                <Field label="run_id" value={detail.run_id} />
                <Field label="Claimed by" value={detail.claimed_by} />
              </div>

              <Field label="Idempotency key" value={detail.idempotency_key} mono copyable />
              <Field label="Content hash" value={detail.content_hash} mono copyable />
              <Field label="Queue id" value={detail.id} mono copyable />

              <div className="flex gap-2 pt-2">
                <Button
                  variant="outline"
                  onClick={() => {
                    setToDelete(detail);
                    setDetail(null);
                  }}
                  className="gap-2"
                >
                  <Trash2 className="size-4" />
                  Discard
                </Button>
              </div>
            </div>
          )}
        </SheetContent>
      </Sheet>

      <DeleteConfirmationModal
        isOpen={!!toDelete}
        onClose={() => setToDelete(null)}
        onConfirm={handleDelete}
        title="Discard queued memory"
        description="This memory has not been stored anywhere else. Discarding it destroys the only copy."
        itemName={toDelete?.text?.slice(0, 80) ?? ""}
        confirmButtonText="Discard"
      />
    </div>
  );
}
