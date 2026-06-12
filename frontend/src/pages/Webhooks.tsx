// Webhooks page: inbound + outbound webhook list, create/edit form, delivery log.
import { useState } from "react";
import { WebhookList } from "../components/webhooks/WebhookList";
import { WebhookForm } from "../components/webhooks/WebhookForm";
import { WebhookDeliveries } from "../components/webhooks/WebhookDeliveries";

export default function Webhooks() {
  const [selectedId, setSelectedId] = useState<number | null>(null);

  return (
    <div>
      <h1 className="page-title">Webhooks</h1>
      <div className="layout-2">
        <div>
          <WebhookList
            selectedId={selectedId}
            onSelect={setSelectedId}
            onNew={() => setSelectedId(null)}
          />
          <div style={{ marginTop: 16 }}>
            <WebhookDeliveries webhookId={selectedId} />
          </div>
        </div>
        <WebhookForm
          webhookId={selectedId}
          onSaved={() => setSelectedId(null)}
        />
      </div>
    </div>
  );
}
