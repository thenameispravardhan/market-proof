import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AlertsPanel } from "../components/trade/ChartWidgets";
import { DEFAULT_FLOW_ALERTS } from "../components/trade/flowAlerts";

describe("AlertsPanel › Order flow", () => {
  it("switches order-flow alerts on and edits a threshold", () => {
    const onFlow = vi.fn();
    const noop = () => undefined;
    render(<AlertsPanel symbolName="NIFTY" alerts={[]} log={[]} fmt={String} onCreate={noop} onEdit={noop} onToggle={noop}
      onDelete={noop} onClearLog={noop} flow={DEFAULT_FLOW_ALERTS} onFlow={onFlow} />);
    fireEvent.click(screen.getByTestId("alerts-flow-tab"));
    fireEvent.click(screen.getByTestId("flow-alert-on"));
    expect(onFlow).toHaveBeenLastCalledWith({ ...DEFAULT_FLOW_ALERTS, on: true });
    const print = screen.getByTestId("flow-alert-print") as HTMLInputElement;
    fireEvent.change(print, { target: { value: "5000" } });
    fireEvent.blur(print);
    expect(onFlow).toHaveBeenLastCalledWith({ ...DEFAULT_FLOW_ALERTS, bigPrint: 5000 });
  });

  it("has no order-flow tab when the chart gives no config", () => {
    const noop = () => undefined;
    render(<AlertsPanel symbolName="SBIN" alerts={[]} log={[]} fmt={String} onCreate={noop} onEdit={noop} onToggle={noop}
      onDelete={noop} onClearLog={noop} />);
    expect(screen.queryByTestId("alerts-flow-tab")).toBeNull();
  });
});
