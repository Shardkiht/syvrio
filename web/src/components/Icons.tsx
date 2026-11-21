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

export function ArchiveIcon(props: IconProps) {
  return <Svg {...props}><path d="M4 8h16v12H4zM3 4h18v4H3zM9 12h6" /></Svg>;
}

export function SearchIcon(props: IconProps) {
  return <Svg {...props}><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4.5 4.5" /></Svg>;
}

export function RefreshIcon(props: IconProps) {
  return <Svg {...props}><path d="M20 5v5h-5M19.5 9a8 8 0 1 0 .1 6" /></Svg>;
}

export function CheckIcon(props: IconProps) {
  return <Svg {...props}><path d="m5 12 4.5 4.5L19 7" /></Svg>;
}

export function EditIcon(props: IconProps) {
  return <Svg {...props}><path d="m14.5 5.5 4 4M4 20l4.7-1 12-12a2.8 2.8 0 0 0-4-4l-12 12L4 20Z" /></Svg>;
}

export function UndoIcon(props: IconProps) {
  return <Svg {...props}><path d="m9 4-5 5 5 5M5 9h9a6 6 0 0 1 0 12" /></Svg>;
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

export function InboxIcon(props: IconProps) {
  return <Svg {...props}>
    <path d="m3.5 13.5 3.2-8h10.6l3.2 8V18a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18Z" />
    <path d="M4 13.5h4.5l1.5 3h4l1.5-3H20" />
  </Svg>;
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
      <rect x="3.5" y="3.5" width="17" height="17" rx="2" fill="currentColor" stroke="none" />
    </Svg>
  );
}
