import { useEffect, useRef, useState } from 'react';

interface AnimatedNumberProps {
  value: number;
  digits?: number;
  prefix?: string;
  suffix?: string;
}

export default function AnimatedNumber({ value, digits = 2, prefix = '', suffix = '' }: AnimatedNumberProps) {
  const [display, setDisplay] = useState<number>(value);
  const fromRef = useRef<number>(value);

  useEffect(() => {
    const from = fromRef.current;
    const to = value;
    const start = performance.now();
    const duration = 560;
    let raf = 0;

    const frame = (now: number) => {
      const p = Math.min(1, (now - start) / duration);
      const ease = 1 - (1 - p) ** 3;
      const next = from + (to - from) * ease;
      setDisplay(next);
      if (p < 1) {
        raf = requestAnimationFrame(frame);
      } else {
        fromRef.current = to;
      }
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [value]);

  return <span>{`${prefix}${display.toFixed(digits)}${suffix}`}</span>;
}
