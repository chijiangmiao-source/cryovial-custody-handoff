import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OpenHandoffPanel, codeFromUrl, shareLink } from "./OpenHandoffPanel";

describe("codeFromUrl", () => {
  it("从 ?code= 读取并归一化为大写", () => {
    window.history.replaceState(null, "", "/?code=abcd2345");
    expect(codeFromUrl()).toBe("ABCD2345");
  });

  it("无参数时返回空串", () => {
    window.history.replaceState(null, "", "/");
    expect(codeFromUrl()).toBe("");
  });
});

describe("shareLink", () => {
  it("生成同源 ?code= 链接", () => {
    expect(shareLink("ABCD2345")).toMatch(/\?code=ABCD2345$/);
  });
});

describe("OpenHandoffPanel", () => {
  it("输入小写码并提交时，归一化为大写后回调", async () => {
    const onOpen = (..._args: unknown[]) => {};
    const spy = vi.fn(onOpen);
    render(<OpenHandoffPanel current="" onOpen={spy} />);
    await userEvent.type(screen.getByTestId("open-handoff-input"), " abcd2345 ");
    await userEvent.click(screen.getByTestId("open-handoff-button"));
    expect(spy).toHaveBeenCalledWith("ABCD2345");
  });

  it("存在当前交接时展示可分享链接", () => {
    render(<OpenHandoffPanel current="ABCD2345" onOpen={() => {}} />);
    expect(screen.getByTestId("share-link").textContent).toContain("code=ABCD2345");
  });
});
