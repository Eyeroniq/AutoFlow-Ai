import {
  AudioLines,
  Bot,
  Braces,
  Eye,
  Box,
  Brain,
  Cpu,
  FileText,
  Filter,
  Gauge,
  GitBranch,
  Globe,
  Inbox,
  ListCollapse,
  LogIn,
  LogOut,
  type LucideIcon,
  Mail,
  MessageSquare,
  Newspaper,
  NotebookPen,
  NotebookTabs,
  Plug,
  Repeat,
  Route,
  Rss,
  ScanText,
  Search,
  Send,
  Sparkles,
  Table,
  TableProperties,
  Tags,
  TextQuote,
  Timer,
  Type,
  Wind,
  Zap,
} from "lucide-react";

// Icon names come from the node registry (GET /api/nodes `icon`).
const ICONS: Record<string, LucideIcon> = {
  sparkles: Sparkles,
  zap: Zap,
  route: Route,
  cpu: Cpu,
  bot: Bot,
  brain: Brain,
  mail: Mail,
  inbox: Inbox,
  globe: Globe,
  "log-in": LogIn,
  "log-out": LogOut,
  type: Type,
  "git-branch": GitBranch,
  timer: Timer,
  "file-text": FileText,
  "scan-text": ScanText,
  "text-quote": TextQuote,
  tags: Tags,
  repeat: Repeat,
  filter: Filter,
  "list-collapse": ListCollapse,
  rss: Rss,
  newspaper: Newspaper,
  send: Send,
  "message-square": MessageSquare,
  "audio-lines": AudioLines,
  search: Search,
  braces: Braces,
  wind: Wind,
  gauge: Gauge,
  plug: Plug,
  eye: Eye,
  "notebook-pen": NotebookPen,
  "notebook-tabs": NotebookTabs,
  table: Table,
  "table-properties": TableProperties,
};

export function NodeIcon({ name, className = "size-4" }: { name?: string; className?: string }) {
  const Icon = (name && ICONS[name]) || Box;
  return <Icon className={className} aria-hidden />;
}

export interface CategoryStyle {
  /** Card accent bar. */
  accent: string;
  /** Icon tile. */
  tile: string;
  /** MiniMap color. */
  color: string;
}

// Literal class names so Tailwind picks them up.
const CATEGORY: Record<string, CategoryStyle> = {
  io: { accent: "bg-emerald-500", tile: "bg-emerald-50 text-emerald-700", color: "#10b981" },
  logic: { accent: "bg-amber-500", tile: "bg-amber-50 text-amber-700", color: "#f59e0b" },
  ai: { accent: "bg-violet-500", tile: "bg-violet-50 text-violet-700", color: "#8b5cf6" },
  integration: { accent: "bg-sky-500", tile: "bg-sky-50 text-sky-700", color: "#0ea5e9" },
  documents: { accent: "bg-rose-500", tile: "bg-rose-50 text-rose-700", color: "#f43f5e" },
  lists: { accent: "bg-teal-500", tile: "bg-teal-50 text-teal-700", color: "#14b8a6" },
  sources: { accent: "bg-orange-500", tile: "bg-orange-50 text-orange-700", color: "#f97316" },
  audio: { accent: "bg-fuchsia-500", tile: "bg-fuchsia-50 text-fuchsia-700", color: "#d946ef" },
};

export function categoryStyle(category?: string): CategoryStyle {
  return (category && CATEGORY[category]) || { accent: "bg-slate-400", tile: "bg-slate-100 text-slate-600", color: "#94a3b8" };
}
