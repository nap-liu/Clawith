import { useCallback, useLayoutEffect, useMemo, useState } from "react";

type CompanyTourStep = {
  selector: string;
  title: string;
  body: string;
  pad?: number;
  radius?: number;
};

type TourRect = {
  left: number;
  top: number;
  width: number;
  height: number;
  radius: number;
};

type TourCardPlacement = "right" | "left" | "below" | "above";

const clamp = (value: number, min: number, max: number) =>
  Math.min(Math.max(value, min), Math.max(min, max));

export default function CompanyTourOverlay({
  assistantId,
  isChinese,
  onDone,
}: {
  assistantId: string;
  isChinese: boolean;
  onDone: () => void;
}) {
  const [step, setStep] = useState(0);
  const [spotRect, setSpotRect] = useState<TourRect | null>(null);
  const [cardPos, setCardPos] = useState<{
    left: number;
    top: number;
    placement: TourCardPlacement;
  }>({ left: 24, top: 24, placement: "right" });
  const steps: CompanyTourStep[] = useMemo(
    () => [
      {
        selector: '[data-tour-target="company-switcher"]',
        title: isChinese ? "公司切换" : "Company switcher",
        body: isChinese
          ? "左上角是当前公司。你加入或创建更多公司后，可以在这里切换。"
          : "The top-left area is your current company. Switch here when you join or create more companies.",
        pad: 8,
        radius: 14,
      },
      {
        selector: '[data-tour-target="main-nav"]',
        title: isChinese ? "主要功能" : "Main areas",
        body: isChinese
          ? "从这里进入项目、探索、发布管理和能力市场。"
          : "Open projects, Explore, published content, and the skill market from here.",
        pad: 8,
        radius: 14,
      },
      {
        selector: '[data-tour-target="agent-list"]',
        title: isChinese ? "员工列表" : "Employee list",
        body: isChinese
          ? "这里是公司员工和你可见的 agent。你的私人助理也会在这里。"
          : "This is the employee and agent list you can access. Your private assistant appears here too.",
        pad: 8,
        radius: 14,
      },
      {
        selector: '[data-tour-target="hire-agent"]',
        title: isChinese ? "新增员工" : "Hire new employees",
        body: isChinese
          ? "点击加号可以从 Talent Market 增加新的 agent 员工。"
          : "Use the plus button to add new agent employees from the Talent Market.",
        pad: 7,
        radius: 10,
      },
    ],
    [isChinese],
  );
  const current = steps[step];
  const progress = `${step + 1} / ${steps.length}`;
  const updateTourPosition = useCallback(() => {
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const viewportPad = 12;
    const element = document.querySelector<HTMLElement>(current.selector);
    const measuredRect = element?.getBoundingClientRect();
    const fallbackWidth = Math.min(280, viewportWidth - viewportPad * 2);
    const fallback = {
      left: viewportPad,
      top: Math.max(72, viewportHeight * 0.18),
      width: fallbackWidth,
      height: 52,
    };
    const sourceRect =
      measuredRect && measuredRect.width > 0 && measuredRect.height > 0
        ? measuredRect
        : fallback;
    const pad = current.pad ?? 8;
    const nextSpot: TourRect = {
      left: clamp(
        sourceRect.left - pad,
        viewportPad / 2,
        viewportWidth - viewportPad,
      ),
      top: clamp(
        sourceRect.top - pad,
        viewportPad / 2,
        viewportHeight - viewportPad,
      ),
      width: Math.min(sourceRect.width + pad * 2, viewportWidth - viewportPad),
      height: Math.min(
        sourceRect.height + pad * 2,
        viewportHeight - viewportPad,
      ),
      radius: current.radius ?? 14,
    };
    if (nextSpot.left + nextSpot.width > viewportWidth - viewportPad / 2) {
      nextSpot.left = Math.max(
        viewportPad / 2,
        viewportWidth - viewportPad / 2 - nextSpot.width,
      );
    }
    if (nextSpot.top + nextSpot.height > viewportHeight - viewportPad / 2) {
      nextSpot.top = Math.max(
        viewportPad / 2,
        viewportHeight - viewportPad / 2 - nextSpot.height,
      );
    }

    const cardWidth = Math.min(320, viewportWidth - viewportPad * 2);
    const cardHeight = 220;
    const gap = 20;
    let placement: TourCardPlacement = "right";
    let cardLeft = nextSpot.left + nextSpot.width + gap;
    let cardTop =
      nextSpot.top + Math.min(24, Math.max(0, nextSpot.height / 2 - 18));

    if (cardLeft + cardWidth > viewportWidth - viewportPad) {
      placement = "left";
      cardLeft = nextSpot.left - cardWidth - gap;
    }
    if (cardLeft < viewportPad) {
      placement = "below";
      cardLeft = clamp(
        nextSpot.left,
        viewportPad,
        viewportWidth - cardWidth - viewportPad,
      );
      cardTop = nextSpot.top + nextSpot.height + gap;
    }
    if (cardTop + cardHeight > viewportHeight - 92) {
      if (nextSpot.top - cardHeight - gap > viewportPad) {
        placement = "above";
        cardTop = nextSpot.top - cardHeight - gap;
        cardLeft = clamp(
          nextSpot.left,
          viewportPad,
          viewportWidth - cardWidth - viewportPad,
        );
      } else {
        cardTop = clamp(cardTop, viewportPad, viewportHeight - cardHeight - 92);
      }
    }

    setSpotRect(nextSpot);
    setCardPos({
      left: Math.round(
        clamp(cardLeft, viewportPad, viewportWidth - cardWidth - viewportPad),
      ),
      top: Math.round(
        clamp(cardTop, viewportPad, viewportHeight - cardHeight - viewportPad),
      ),
      placement,
    });
  }, [current]);

  useLayoutEffect(() => {
    updateTourPosition();
    const raf = window.requestAnimationFrame(updateTourPosition);
    window.addEventListener("resize", updateTourPosition);
    window.addEventListener("scroll", updateTourPosition, true);
    return () => {
      window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", updateTourPosition);
      window.removeEventListener("scroll", updateTourPosition, true);
    };
  }, [updateTourPosition]);

  const next = () => {
    if (step >= steps.length - 1) onDone();
    else setStep((s) => s + 1);
  };
  const previous = () => setStep((s) => Math.max(0, s - 1));
  return (
    <div className="company-tour-overlay">
      {spotRect && (
        <div
          className="tour-spot"
          style={{
            left: spotRect.left,
            top: spotRect.top,
            width: spotRect.width,
            height: spotRect.height,
            borderRadius: spotRect.radius,
          }}
        />
      )}
      <div
        className={`company-tour-card company-tour-card--${cardPos.placement}`}
        style={{ left: cardPos.left, top: cardPos.top }}
      >
        <div className="company-tour-step">{step + 1}</div>
        <h3>{current.title}</h3>
        <p>{current.body}</p>
      </div>
      <div className="company-tour-controls">
        <button type="button" onClick={onDone}>
          {isChinese ? "跳过导览" : "Skip tour"}
        </button>
        <button type="button" onClick={previous} disabled={step === 0}>
          {isChinese ? "上一步" : "Back"}
        </button>
        <div className="company-tour-dots">
          {steps.map((_, i) => (
            <span key={i} className={i === step ? "active" : ""} />
          ))}
        </div>
        <span>{progress}</span>
        <button type="button" className="company-tour-next" onClick={next}>
          {step >= steps.length - 1
            ? isChinese
              ? "完成"
              : "Finish"
            : isChinese
              ? "下一个"
              : "Next"}{" "}
          →
        </button>
      </div>
      {assistantId && (
        <div className="company-tour-hint">
          {isChinese
            ? "导览结束后，会进入你的私人助理对话页。"
            : "After the tour, you will land in your private assistant chat."}
        </div>
      )}
    </div>
  );
}
