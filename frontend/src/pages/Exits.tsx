// Exits page: the Exit Manager — every entry/exit knob, frontend-only.
import { ExitManager } from "../components/exits/ExitManager";

export default function Exits() {
  return (
    <div>
      <h1 className="page-title">Exit Manager</h1>
      <ExitManager />
    </div>
  );
}
