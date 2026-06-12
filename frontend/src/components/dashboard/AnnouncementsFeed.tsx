// Announcements feed: the most recent filings from BSE/NSE. Each row
// shows the symbol, event badge, headline, and filed-at time. We keep
// it tight — a "View PDF" link opens the underlying filing.

import { useAnnouncements } from "../../hooks/useApi";
import { ApiClientError } from "../../api/client";
import { SkeletonList } from "./Skeleton";

function fmtTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export function AnnouncementsFeed() {
  const { data, isLoading, error } = useAnnouncements(20);

  return (
    <div className="widget" data-testid="announcements-feed">
      <h3>Announcements</h3>
      {isLoading ? (
        <SkeletonList rows={4} />
      ) : error ? (
        <p className="empty">
          {error instanceof ApiClientError && error.status === 404
            ? "Endpoint not available yet — backend doesn't expose announcements."
            : `Failed to load: ${error.message}`}
        </p>
      ) : !data || data.length === 0 ? (
        <p className="empty">No recent announcements.</p>
      ) : (
        <div className="list">
          {data.map((a) => (
            <div className="item" key={a.id}>
              <div className="head">
                <span className="symbol">{a.symbol}</span>
                <span className="badge info">{a.event_type}</span>
              </div>
              <div className="body">{a.headline}</div>
              <div className="body reason">
                {a.exchange} · {fmtTime(a.filed_at)}
                {a.pdf_url && (
                  <>
                    {" · "}
                    <a href={a.pdf_url} target="_blank" rel="noreferrer">PDF</a>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
