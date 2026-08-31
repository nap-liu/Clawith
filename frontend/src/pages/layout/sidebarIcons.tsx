import {
  IconBell,
  IconChevronsLeft,
  IconChevronsRight,
  IconHome,
  IconLogout,
  IconMoon,
  IconPlus,
  IconSettings,
  IconSun,
  IconUser,
  IconWorld,
} from "@tabler/icons-react";

export const SidebarIcons = {
  home: <IconHome size={16} stroke={1.5} />,
  plus: <IconPlus size={16} stroke={1.5} />,
  settings: <IconSettings size={16} stroke={1.5} />,
  user: <IconUser size={16} stroke={1.5} />,
  sun: <IconSun size={16} stroke={1.5} />,
  moon: <IconMoon size={16} stroke={1.5} />,
  logout: <IconLogout size={16} stroke={1.5} />,
  globe: <IconWorld size={16} stroke={1.5} />,
  collapse: <IconChevronsLeft size={16} stroke={1.5} />,
  expand: <IconChevronsRight size={16} stroke={1.5} />,
  bell: <IconBell size={16} stroke={1.5} />,
};

/** UI locales: native endonym per row. */
export const APP_UI_LANGUAGES: { code: string; nativeLabel: string }[] = [
  { code: "zh", nativeLabel: "中文" },
  { code: "en", nativeLabel: "English" },
];

export function resolveUiLangCode(lang: string | undefined): string {
  if (!lang) return "en";
  if (lang.startsWith("zh")) return "zh";
  return "en";
}
