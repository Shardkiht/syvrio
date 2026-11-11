import type { ReactNode } from 'react';

interface IconProps {
  size?: number;
  className?: string;
}

/** One stroke style for every chrome icon, so the shell reads as one set. */
function Svg({ size = 18, className, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export function PanelIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="4.5" width="18" height="15" rx="3" />
      <path d="M9.5 4.5v15" />
    </Svg>
  );
}

export function PlusIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 5.5v13M5.5 12h13" />
    </Svg>
  );
}

export function CommandsIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3.5" y="4.5" width="17" height="15" rx="3" />
      <path d="M8 9.5l2.5 2.5L8 14.5M12.5 14.5H16" />
    </Svg>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 7.5h7M15 7.5h5M4 16.5h5M13 16.5h7" />
      <circle cx="13" cy="7.5" r="2.2" />
      <circle cx="11" cy="16.5" r="2.2" />
    </Svg>
  );
}

export function ArrowUpIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 19V6M6 12l6-6 6 6" />
    </Svg>
  );
}

export function StopIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="8" y="8" width="8" height="8" rx="2" fill="currentColor" stroke="none" />
    </Svg>
  );
}
