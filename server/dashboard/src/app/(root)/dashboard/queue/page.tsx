"use client";

import { useState } from "react";
import { AlertTriangle, RefreshCw, Trash2 } from "lucide-react";
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
  created_at: string | null;
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

  const handleRetry = async () => {
    setRetrying(true);
    try {
      await api.post(PENDING_ENDPOINTS.RETRY, {});
      toast({ title: "Queue re-armed", variant: "success" });
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
      render: (value: PendingItem["state"]) => (
        <Badge variant="outline" className={`capitalize ${STATE_STYLE[value]}`}>
          {value}
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
        <span
          className="text-onSurface-default-secondary line-clamp-2"
          title={row.last_error ?? undefined}
        >
          {value ?? "-"}
        </span>
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
        <Button onClick={handleRetry} disabled={retrying} className="gap-2">
          <RefreshCw className={`size-4 ${retrying ? "animate-spin" : ""}`} />
          Retry all now
        </Button>
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
          <DataTable data={data.items} columns={columns} getRowKey={(row) => row.id} />
        </Card>
      )}

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
