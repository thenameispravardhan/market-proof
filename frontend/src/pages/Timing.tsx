// Timing page: per-layer execution latency — where the wall-clock goes.
import { ExecutionTiming } from "../components/timing/ExecutionTiming";

export default function Timing() {
  return (
    <div>
      <h1 className="page-title">Execution Timing</h1>
      <ExecutionTiming />
    </div>
  );
}
