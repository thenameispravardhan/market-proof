// ChannelForm — create or update a notification channel.
// Shows kind-specific fields.
import { useEffect, useState } from "react";
import {
  useNotificationChannels,
  useCreateNotificationChannel,
  useUpdateNotificationChannel,
} from "../../hooks/useApi";

type Kind = "telegram" | "discord" | "email" | "webhook";

interface Props {
  channelId: number | null;
  onSaved?: () => void;
}

export function ChannelForm({ channelId, onSaved }: Props) {
  const { data: channels } = useNotificationChannels();
  const channel = channels?.find((c) => c.id === channelId) ?? null;
  const create = useCreateNotificationChannel();
  const update = useUpdateNotificationChannel(channelId);

  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind>("telegram");
  const [eventsFilter, setEventsFilter] = useState("*");
  const [enabled, setEnabled] = useState(true);
  // kind-specific
  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState("587");
  const [smtpUser, setSmtpUser] = useState("");
  const [smtpPass, setSmtpPass] = useState("");
  const [fromAddr, setFromAddr] = useState("");
  const [toAddrs, setToAddrs] = useState("");
  const [webhookOutUrl, setWebhookOutUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (channel) {
      setName(channel.name);
      setKind(channel.kind as Kind);
      setEventsFilter(channel.events_filter ?? "*");
      setEnabled(channel.enabled);
      const cfg = channel.config as Record<string, string>;
      setBotToken(cfg.bot_token ?? "");
      setChatId(cfg.chat_id ?? "");
      setWebhookUrl(cfg.webhook_url ?? "");
      setSmtpHost(cfg.smtp_host ?? "");
      setSmtpPort(String(cfg.smtp_port ?? 587));
      setSmtpUser(cfg.username ?? "");
      setSmtpPass(cfg.password ? "●●●●" : "");
      setFromAddr(cfg.from_addr ?? "");
      setToAddrs(Array.isArray(cfg.to_addrs) ? cfg.to_addrs.join(", ") : "");
      setWebhookOutUrl(cfg.url ?? "");
    } else {
      setName(""); setKind("telegram"); setEventsFilter("*"); setEnabled(true);
      setBotToken(""); setChatId(""); setWebhookUrl(""); setSmtpHost("");
      setSmtpPort("587"); setSmtpUser(""); setSmtpPass(""); setFromAddr("");
      setToAddrs(""); setWebhookOutUrl("");
    }
    setError(null);
  }, [channel]);

  const buildConfig = (): Record<string, unknown> => {
    switch (kind) {
      case "telegram": return { bot_token: botToken, chat_id: chatId };
      case "discord": return { webhook_url: webhookUrl };
      case "email": return {
        smtp_host: smtpHost,
        smtp_port: Number(smtpPort),
        username: smtpUser,
        password: smtpPass,
        from_addr: fromAddr,
        to_addrs: toAddrs.split(",").map((s) => s.trim()).filter(Boolean),
      };
      case "webhook": return { url: webhookOutUrl };
      default: return {};
    }
  };

  const handleSave = async () => {
    setError(null);
    const config = buildConfig();
    const body = { name, kind, config, events_filter: eventsFilter, enabled };
    try {
      if (channel) {
        await update.mutateAsync(body as never);
      } else {
        await create.mutateAsync(body as never);
      }
      onSaved?.();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="widget" data-testid="channel-form">
      <h3>{channel ? `Edit — ${channel.name}` : "New Channel"}</h3>
      <div className="field">
        <label htmlFor="ch-name">Name</label>
        <input id="ch-name" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="ch-kind">Kind</label>
        <select id="ch-kind" value={kind} onChange={(e) => setKind(e.target.value as Kind)} disabled={!!channel}>
          <option value="telegram">Telegram</option>
          <option value="discord">Discord</option>
          <option value="email">Email</option>
          <option value="webhook">Webhook</option>
        </select>
      </div>

      {kind === "telegram" && (
        <>
          <div className="field">
            <label htmlFor="tg-token">Bot token</label>
            <input id="tg-token" type="password" value={botToken} onChange={(e) => setBotToken(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="field">
            <label htmlFor="tg-chat">Chat ID</label>
            <input id="tg-chat" value={chatId} onChange={(e) => setChatId(e.target.value)} />
          </div>
        </>
      )}
      {kind === "discord" && (
        <div className="field">
          <label htmlFor="dc-url">Webhook URL</label>
          <input id="dc-url" value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)} />
        </div>
      )}
      {kind === "email" && (
        <>
          <div className="field-row">
            <div className="field">
              <label htmlFor="em-host">SMTP host</label>
              <input id="em-host" value={smtpHost} onChange={(e) => setSmtpHost(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="em-port">Port</label>
              <input id="em-port" type="number" value={smtpPort} onChange={(e) => setSmtpPort(e.target.value)} style={{ width: 80 }} />
            </div>
          </div>
          <div className="field">
            <label htmlFor="em-user">Username</label>
            <input id="em-user" value={smtpUser} onChange={(e) => setSmtpUser(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="em-pass">Password</label>
            <input id="em-pass" type="password" value={smtpPass} onChange={(e) => setSmtpPass(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="field">
            <label htmlFor="em-from">From address</label>
            <input id="em-from" value={fromAddr} onChange={(e) => setFromAddr(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="em-to">To addresses (comma-separated)</label>
            <input id="em-to" value={toAddrs} onChange={(e) => setToAddrs(e.target.value)} />
          </div>
        </>
      )}
      {kind === "webhook" && (
        <div className="field">
          <label htmlFor="wh-url">Webhook URL</label>
          <input id="wh-url" value={webhookOutUrl} onChange={(e) => setWebhookOutUrl(e.target.value)} />
        </div>
      )}

      <div className="field">
        <label htmlFor="ch-events">Events filter (comma-separated or *)</label>
        <input id="ch-events" value={eventsFilter} onChange={(e) => setEventsFilter(e.target.value)} placeholder="signal,trade,risk_halt,error" />
      </div>
      <div className="field">
        <label>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {" "}Enabled
        </label>
      </div>
      {error && <p className="pnl-neg">{error}</p>}
      <button
        className="primary"
        onClick={handleSave}
        disabled={create.isPending || update.isPending || !name}
        data-testid="save-channel"
      >
        {create.isPending || update.isPending ? "Saving…" : channel ? "Update" : "Create"}
      </button>
    </div>
  );
}
