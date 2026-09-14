import React from "react";
import type { ToolCard as Card } from "../api/client";
import type { CheckState } from "../useToolChecks";
import { ToolCard } from "../components/ToolCard";

const ORDER = ["hermes", "opencode", "codex", "t3"];

export function Overview({
  cards,
  updateDisabled,
  disableReason,
  checkStates,
  planningId,
  disconnected,
  onCheck,
  onPlan,
}: {
  cards: Card[];
  updateDisabled: boolean;
  disableReason: string;
  checkStates: Record<string, CheckState>;
  planningId: string;
  disconnected: boolean;
  onCheck: (toolId: string) => void;
  onPlan: (toolId: string) => void;
}): React.ReactElement {
  const sorted = [...cards].sort((a, b) => ORDER.indexOf(a.id) - ORDER.indexOf(b.id));
  return (
    <section aria-label="Tool overview">
      <div className="card-grid">
        {sorted.map((c) => (
          <ToolCard
            key={c.id}
            card={c}
            updateDisabled={updateDisabled}
            disableReason={disableReason}
            onCheck={() => onCheck(c.id)}
            onPlan={() => onPlan(c.id)}
            checking={checkStates[c.id]?.kind === "checking" || checkStates[c.id]?.kind === "pending"}
            checkState={checkStates[c.id]}
            planning={planningId === c.id}
            disconnected={disconnected}
          />
        ))}
      </div>
    </section>
  );
}
