// Prompts page: layout is
//   [ PromptList | Editor          ]
//   [            | History / Preview ]

import { useState } from "react";
import { PromptList } from "../components/prompts/PromptList";
import { PromptEditor } from "../components/prompts/PromptEditor";
import { PromptHistory } from "../components/prompts/PromptHistory";
import { PromptPreview } from "../components/prompts/PromptPreview";

export default function Prompts() {
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <div>
      <h1 className="page-title">Prompts</h1>
      <div className="layout-2">
        <PromptList selected={selected} onSelect={setSelected} />
        <div>
          <PromptEditor eventType={selected} />
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginTop: 16 }}>
            <PromptHistory eventType={selected} />
            <PromptPreview eventType={selected} />
          </div>
        </div>
      </div>
    </div>
  );
}
