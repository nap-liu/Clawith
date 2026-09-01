import { useCallback, useRef } from 'react';

import ToggleSwitch from './ToggleSwitch';
import './DivergenceSlider.css';

const MIN = 0;
const MAX = 2;
const STEP = 0.1;
const FALLBACK_VALUE = 0.7;

type Props = {
    value: number | null;
    onChange: (value: number | null) => void;
    label?: string;
    inheritedLabel?: string;
    lowLabel?: string;
    middleLabel?: string;
    highLabel?: string;
    disabled?: boolean;
};

function normalizeValue(value: number) {
    const clamped = Math.max(MIN, Math.min(MAX, value));
    return Number((Math.round(clamped / STEP) * STEP).toFixed(1));
}

export default function DivergenceSlider({
    value,
    onChange,
    label = '想象力',
    inheritedLabel = '继承默认值',
    lowLabel = '稳定',
    middleLabel = '均衡',
    highLabel = '丰富',
    disabled = false,
}: Props) {
    const trackRef = useRef<HTMLDivElement>(null);
    const inherited = value === null;
    const sliderValue = normalizeValue(value ?? FALLBACK_VALUE);
    const sliderDisabled = disabled || inherited;
    const progress = ((sliderValue - MIN) / (MAX - MIN)) * 100;

    const updateFromPointer = useCallback((clientX: number) => {
        const bounds = trackRef.current?.getBoundingClientRect();
        if (!bounds || bounds.width === 0) return;
        const ratio = (clientX - bounds.left) / bounds.width;
        onChange(normalizeValue(MIN + ratio * (MAX - MIN)));
    }, [onChange]);

    const changeBy = (delta: number) => onChange(normalizeValue(sliderValue + delta));

    return (
        <div className={`divergence-control${inherited ? ' is-inherited' : ''}${disabled ? ' is-disabled' : ''}`}>
            <div className="divergence-control__header">
                <div className="divergence-control__title-group">
                    <span className="divergence-control__title">{label}</span>
                    <output className="divergence-control__value" aria-live="polite">
                        {inherited ? '—' : sliderValue.toFixed(1)}
                    </output>
                </div>
                <div className="divergence-control__inherit">
                    <span>{inheritedLabel}</span>
                    <ToggleSwitch
                        checked={inherited}
                        disabled={disabled}
                        onChange={(checked) => onChange(checked ? null : sliderValue)}
                        ariaLabel={inheritedLabel}
                    />
                </div>
            </div>

            <div
                ref={trackRef}
                className="divergence-control__interaction"
                role="slider"
                tabIndex={sliderDisabled ? -1 : 0}
                aria-label={label}
                aria-valuemin={MIN}
                aria-valuemax={MAX}
                aria-valuenow={inherited ? undefined : sliderValue}
                aria-valuetext={inherited ? inheritedLabel : sliderValue.toFixed(1)}
                aria-disabled={sliderDisabled}
                onPointerDown={(event) => {
                    if (sliderDisabled) return;
                    event.currentTarget.setPointerCapture(event.pointerId);
                    updateFromPointer(event.clientX);
                }}
                onPointerMove={(event) => {
                    if (sliderDisabled || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
                    updateFromPointer(event.clientX);
                }}
                onKeyDown={(event) => {
                    if (sliderDisabled) return;
                    if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') changeBy(-STEP);
                    else if (event.key === 'ArrowRight' || event.key === 'ArrowUp') changeBy(STEP);
                    else if (event.key === 'PageDown') changeBy(-0.5);
                    else if (event.key === 'PageUp') changeBy(0.5);
                    else if (event.key === 'Home') onChange(MIN);
                    else if (event.key === 'End') onChange(MAX);
                    else return;
                    event.preventDefault();
                }}
            >
                <div className="divergence-control__rail">
                    <div className="divergence-control__fill" style={{ width: `${progress}%` }} />
                    {[0, 1, 2].map((tick) => (
                        <span
                            key={tick}
                            className="divergence-control__tick"
                            style={{ left: `${tick * 50}%` }}
                            aria-hidden="true"
                        />
                    ))}
                    <span
                        className="divergence-control__thumb"
                        style={{ left: `${progress}%` }}
                        aria-hidden="true"
                    />
                </div>
            </div>

            <div className="divergence-control__scale" aria-hidden="true">
                <span><b>0</b>{lowLabel}</span>
                <span><b>1</b>{middleLabel}</span>
                <span><b>2</b>{highLabel}</span>
            </div>
        </div>
    );
}
