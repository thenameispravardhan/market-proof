// ChannelForm — create or update a notification channel.
// Shows kind-specific fields.
import { useEffect, useRef, useState } from "react";
import {
  useNotificationChannels,
  useCreateNotificationChannel,
  useUpdateNotificationChannel,
} from "../../hooks/useApi";

type Kind = "telegram" | "discord" | "email" | "webhook";

// The events a channel can subscribe to (app/notifications/manager.py).
// "trade" covers both entries and exits.
const EVENT_TYPES: { value: string; label: string }[] = [
  { value: "signal", label: "New AI signals" },
  { value: "trade", label: "Trades opened and closed" },
  { value: "risk_halt", label: "Risk halts (trading paused)" },
  { value: "error", label: "System errors" },
  { value: "report", label: "Daily health report" },
];

// GET masks secrets as "***". Never put the mask in an input: it would be
// sent back on save. The field starts blank, and blank means "keep it".
const isMasked = (v: unknown) => v === "***";

interface Props {
  channelId: number | null;
  onSaved?: (id: number) => void;
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
  const [savedMsg, setSavedMsg] = useState<string | null>(null);
  const hasStoredSecret = (key: string) =>
    !!channel && isMasked((channel.config as Record<string, unknown>)[key]);

  useEffect(() => {
    if (channel) {
      setName(channel.name);
      setKind(channel.kind as Kind);
      setEventsFilter(channel.events_filter ?? "*");
      setEnabled(channel.enabled);
      const cfg = channel.config as Record<string, string>;
      setBotToken(isMasked(cfg.bot_token) ? "" : cfg.bot_token ?? "");
      setChatId(cfg.chat_id ?? "");
      setWebhookUrl(cfg.webhook_url ?? "");
      setSmtpHost(cfg.smtp_host ?? "");
      setSmtpPort(String(cfg.smtp_port ?? 587));
      setSmtpUser(cfg.username ?? "");
      setSmtpPass(isMasked(cfg.password) ? "" : cfg.password ?? "");
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

  // Keep the "Saved" line through the refetch that follows a save; clear it
  // only when the operator moves to a different channel.
  const savedFor = useRef<number | null>(null);
  useEffect(() => {
    if (savedFor.current !== channelId) setSavedMsg(null);
  }, [channelId]);

  // Events filter as checkboxes. "*" = everything; anything not in the list
  // (hand-typed in an older build) is kept as-is.
  const selectedEvents = eventsFilter.trim() === "*" || eventsFilter.trim() === ""
    ? null
    : eventsFilter.split(",").map((e) => e.trim().toLowerCase()).filter(Boolean);
  const toggleEvent = (value: string) => {
    const current = selectedEvents ?? [];
    const next = current.includes(value)
      ? current.filter((e) => e !== value)
      : [...current, value];
    setEventsFilter(next.length ? next.join(",") : "*");
  };

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
      const saved = channel
        ? await update.mutateAsync(body as never)
        : await create.mutateAsync(body as never);
      savedFor.current = saved.id;
      setSavedMsg(channel ? `Saved "${name}".` : `Created "${name}". Use Test to check it.`);
      onSaved?.(saved.id);
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
            <input id="tg-token" type="password" value={botToken} onChange={(e) => setBotToken(e.target.value)} autoComplete="new-password" placeholder={hasStoredSecret("bot_token") ? "Saved. Leave blank to keep it" : "From @BotFather"} />
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
            <input id="em-pass" type="password" value={smtpPass} onChange={(e) => setSmtpPass(e.target.value)} autoComplete="new-password" placeholder={hasStoredSecret("password") ? "Saved. Leave blank to keep it" : ""} />
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

      <div className="field" data-testid="channel-events">
        <label>Send me</label>
        <label style={{ fontWeight: "normal" }}>
          <input
            type="checkbox"
            checked={selectedEvents === null}
            onChange={() => setEventsFilter(selectedEvents === null ? "signal,trade,risk_halt,error" : "*")}
          />{" "}Everything
        </label>
        {EVENT_TYPES.map((ev) => (
          <label key={ev.value} style={{ fontWeight: "normal" }}>
            <input
              type="checkbox"
              checked={selectedEvents === null || selectedEvents.includes(ev.value)}
              disabled={selectedEvents === null}
              onChange={() => toggleEvent(ev.value)}
            />{" "}{ev.label}
          </label>
        ))}
      </div>
      <div className="field">
        <label>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          {" "}Enabled
        </label>
      </div>
      {error && <p className="pnl-neg">{error}</p>}
      {savedMsg && !error && <p className="pnl-pos">{savedMsg}</p>}
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
