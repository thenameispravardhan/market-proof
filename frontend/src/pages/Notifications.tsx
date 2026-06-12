// Notifications page: channel list + create/edit form.
import { useState } from "react";
import { ChannelList } from "../components/notifications/ChannelList";
import { ChannelForm } from "../components/notifications/ChannelForm";

export default function Notifications() {
  const [selectedId, setSelectedId] = useState<number | null>(null);

  return (
    <div>
      <h1 className="page-title">Notifications</h1>
      <div className="layout-2">
        <ChannelList
          selectedId={selectedId}
          onSelect={setSelectedId}
          onNew={() => setSelectedId(null)}
        />
        <ChannelForm
          channelId={selectedId}
          onSaved={() => setSelectedId(null)}
        />
      </div>
    </div>
  );
}
