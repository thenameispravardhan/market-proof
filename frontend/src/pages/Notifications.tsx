// Notifications page: channel list + create/edit form.
import { useState } from "react";
import { ChannelList } from "../components/notifications/ChannelList";
import { ChannelForm } from "../components/notifications/ChannelForm";

export default function Notifications() {
  const [selectedId, setSelectedId] = useState<number | null>(null);

  return (
    <div>
      <h1 className="page-title">Notifications</h1>
      <p className="text-dim" style={{ marginBottom: 16, maxWidth: 720 }}>
        Get pinged on Telegram, Discord, email or a webhook when the bot finds a
        signal, trades, or pauses itself. Add a channel, pick what it should
        send, then press Test.
      </p>
      <div className="layout-2">
        <ChannelList
          selectedId={selectedId}
          onSelect={setSelectedId}
          onNew={() => setSelectedId(null)}
        />
        <ChannelForm
          channelId={selectedId}
          onSaved={(id) => setSelectedId(id)}
        />
      </div>
    </div>
  );
}
