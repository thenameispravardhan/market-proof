import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IndicatorPicker, IndicatorSettings, SaveTemplateDialog } from "../components/trade/IndicatorDialogs";
import { Num } from "../components/trade/chartUi";
import { newInstance, type IndicatorInstance } from "../components/trade/indicatorCatalog";

function Harness({ start, onSaveDefault = () => {}, onClose = () => {} }: { start: IndicatorInstance; onSaveDefault?: (i: IndicatorInstance) => void; onClose?: () => void }) {
  const [inst, setInst] = useState(start);
  const [open, setOpen] = useState(true);
  return (
    <>
      <output data-testid="state">{JSON.stringify(inst)}</output>
      {open && <IndicatorSettings inst={inst} onChange={setInst} onClose={() => { setOpen(false); onClose(); }} onSaveDefault={onSaveDefault} />}
    </>
  );
}
const state = () => JSON.parse(screen.getByTestId("state").textContent ?? "{}") as IndicatorInstance;

describe("indicator settings dialog", () => {
  it("Escape closes an open color picker without cancelling the dialog", async () => {
    const user = userEvent.setup();
    render(<Harness start={newInstance("rsi")!} />);
    await user.click(screen.getByRole("tab", { name: "Style" }));
    await user.click(screen.getAllByRole("button", { name: /color$/ })[0]);
    await user.click(screen.getByRole("button", { name: "#F23645" }));
    expect(state().plots[0].color).toBe("#F23645");
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("color-popup")).toBeNull();
    expect(screen.getByTestId("ind-settings")).toBeInTheDocument();
    expect(state().plots[0].color).toBe("#F23645");
    // the next Escape is the dialog's: it cancels and reverts
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("ind-settings")).toBeNull();
    expect(state().plots[0].color).not.toBe("#F23645");
  });

  it("Reset settings refreshes a typed symbol input, and the Defaults menu closes on an outside click", async () => {
    const user = userEvent.setup();
    render(<Harness start={newInstance("ratio")!} />);
    const box = screen.getByRole("textbox", { name: "Symbol" }) as HTMLInputElement;
    await user.clear(box);
    await user.type(box, "nse:banknifty-index{Enter}");
    expect(state().inputs.symbol).toBe("NSE:BANKNIFTY-INDEX");
    await user.click(screen.getByTestId("ind-settings-defaults"));
    await user.click(screen.getByTestId("ind-settings-reset"));
    expect(state().inputs.symbol).toBe("NSE:NIFTY50-INDEX");
    expect((screen.getByRole("textbox", { name: "Symbol" }) as HTMLInputElement).value).toBe("NSE:NIFTY50-INDEX");

    await user.click(screen.getByTestId("ind-settings-defaults"));
    expect(screen.getByTestId("ind-settings-reset")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Style" }));
    expect(screen.queryByTestId("ind-settings-reset")).toBeNull();
  });

  it("Save as default never saves a hidden indicator", async () => {
    const user = userEvent.setup();
    const save = vi.fn();
    render(<Harness start={{ ...newInstance("rsi")!, visible: false }} onSaveDefault={save} />);
    await user.click(screen.getByTestId("ind-settings-defaults"));
    await user.click(screen.getByTestId("ind-settings-save-default"));
    expect(save.mock.calls[0][0].visible).toBe(true);
  });

  it("keeps a visibility range the right way round", async () => {
    const user = userEvent.setup();
    render(<Harness start={newInstance("rsi")!} />);
    await user.click(screen.getByRole("tab", { name: "Visibility" }));
    const to = screen.getByRole("spinbutton", { name: "Minutes to" });
    await user.clear(to);
    await user.type(to, "5");
    const from = screen.getByRole("spinbutton", { name: "Minutes from" });
    await user.clear(from);
    await user.type(from, "15");
    expect(state().vis?.minutes).toEqual({ on: true, min: 15, max: 15 });
    await user.click(screen.getByRole("checkbox", { name: "Show on minutes" }));
    expect(screen.getByRole("spinbutton", { name: "Minutes from" })).toBeDisabled();
  });

  it("does not crash on an instance saved with fewer plot styles than its indicator has", async () => {
    const user = userEvent.setup();
    const inst = newInstance("macd")!;
    render(<Harness start={{ ...inst, plots: inst.plots.slice(0, 1) }} />);
    await user.click(screen.getByRole("tab", { name: "Style" }));
    await user.click(screen.getByRole("checkbox", { name: "Show Signal" }));
    expect(state().plots).toHaveLength(inst.plots.length);
    expect(state().plots[2].visible).toBe(false);
  });
});

describe("number field", () => {
  function N({ min }: { min: number }) {
    const [v, setV] = useState(20);
    return (<><Num value={v} min={min} max={100} onChange={setV} ariaLabel="n" /><output data-testid="v">{v}</output></>);
  }
  it("doesn't snap to the minimum mid-typing, and restores a blank field on blur", async () => {
    const user = userEvent.setup();
    render(<N min={2} />);
    const box = screen.getByRole("spinbutton", { name: "n" }) as HTMLInputElement;
    await user.clear(box);
    await user.type(box, "14");
    expect(screen.getByTestId("v").textContent).toBe("14");
    await user.clear(box);
    fireEvent.blur(box);
    expect(box.value).toBe("14");
    await user.clear(box);
    await user.type(box, "500");
    fireEvent.blur(box);
    expect(box.value).toBe("100");
    expect(screen.getByTestId("v").textContent).toBe("100");
  });
});

describe("indicator picker", () => {
  const props = { favorites: [] as string[], onFav: () => {}, onClose: () => {}, strategies: [], onRunStrategy: () => {}, templates: [], onApplyTemplate: () => {}, intraday: true };
  it("Enter adds the best match, and says what was added", async () => {
    const user = userEvent.setup();
    const add = vi.fn();
    render(<IndicatorPicker {...props} onAdd={add} />);
    await user.type(screen.getByRole("textbox", { name: "Search indicators" }), "ema{Enter}");
    expect(add).toHaveBeenCalledWith("ema");
    expect(screen.getByRole("status").textContent).toContain("Moving Average Exponential");
  });
  it("shows which indicators are favorites", () => {
    render(<IndicatorPicker {...props} favorites={["rsi"]} onAdd={() => {}} />);
    expect(screen.getByRole("button", { name: "Favorite Relative Strength Index" })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("indicator templates", () => {
  it("can't save an empty template", () => {
    const onSave = vi.fn();
    render(<SaveTemplateDialog symbolLabel="RELIANCE" intervalLabel="5 minutes" existing={[]} count={0} onSave={onSave} onClose={() => {}} />);
    fireEvent.change(screen.getByTestId("ind-template-name"), { target: { value: "Mine" } });
    expect(screen.getByTestId("ind-template-save-ok")).toBeDisabled();
    expect(screen.getByText(/Add some indicators/)).toBeInTheDocument();
  });
});

describe("indicator visibility warnings", () => {
  it("says when the chart's interval is excluded", () => {
    const inst = { ...newInstance("ema")!, vis: { minutes: { on: false, min: 1, max: 59 } } } as IndicatorInstance;
    render(<IndicatorSettings inst={inst} interval="5" intervalLabel="5 minutes" onChange={() => {}} onClose={() => {}} onSaveDefault={() => {}} />);
    expect(screen.getByText(/hidden on 5 minutes/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Visibility"));
    expect(screen.getByTestId("ind-vis-warn")).toHaveTextContent("Not shown on this chart's interval");
  });
});
