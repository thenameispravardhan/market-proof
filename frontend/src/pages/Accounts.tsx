// Accounts page: broker account list with secret masking and test button.
import { useState } from "react";
import { BrokerAccountList } from "../components/accounts/BrokerAccountList";
import { BrokerAccountForm } from "../components/accounts/BrokerAccountForm";

export default function Accounts() {
  const [selectedId, setSelectedId] = useState<number | null>(null);

  return (
    <div>
      <h1 className="page-title">Broker Accounts</h1>
      <div className="layout-2">
        <BrokerAccountList
          selectedId={selectedId}
          onSelect={setSelectedId}
          onNew={() => setSelectedId(null)}
        />
        <BrokerAccountForm
          accountId={selectedId}
          onSaved={() => setSelectedId(null)}
        />
      </div>
    </div>
  );
}
