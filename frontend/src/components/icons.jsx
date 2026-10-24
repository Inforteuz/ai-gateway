import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
// Line icons in a single consistent style. 24x24 viewBox, `currentColor`.
// Size is set via CSS (`.ico-chip svg`, `.rail-link svg`, etc.).

const base = {
  width: 20,
  height: 20,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': 'true',
}

const make = (path) => function Icon(props) {
  useLocaleRefresh()
  return <svg {...base} {...props}>{tr(path)}</svg>
}

export const IconHome = make(<><path d="M3 10.5 12 3l9 7.5" /><path d="M5.5 9.5V20h13V9.5" /><path d="M9.5 20v-5.5h5V20" /></>)
export const IconKey = make(<><circle cx="8" cy="14" r="4" /><path d="M11 11.5 20 3M17.5 5.5 20 8M15 8l2 2" /></>)
export const IconLayers = make(<><path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z" /><path d="m3 12.5 9 4.5 9-4.5" /><path d="m3 16.8 9 4.5 9-4.5" /></>)
export const IconChart = make(<><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>)
export const IconCard = make(<><rect x="2.5" y="5" width="19" height="14" rx="3" /><path d="M2.5 10h19" /></>)
export const IconDoc = make(<><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4" /><path d="M9 12h6M9 16h4" /></>)
export const IconZap = make(<path d="M13 2 4 13.5h6L11 22l9-11.5h-6L13 2Z" />)
export const IconShield = make(<><path d="M12 2.5 20 6v6c0 5-3.4 8.3-8 9.5C7.4 20.3 4 17 4 12V6l8-3.5Z" /><path d="m9 12 2 2 4-4" /></>)
export const IconBox = make(<><path d="M20.5 7.5 12 3 3.5 7.5v9L12 21l8.5-4.5v-9Z" /><path d="M3.5 7.5 12 12l8.5-4.5M12 12v9" /></>)
export const IconClock = make(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5.2l3.2 2" /></>)
export const IconLink = make(<><path d="M10 13.5a4 4 0 0 0 5.7 0l2.8-2.8a4 4 0 0 0-5.7-5.7l-1.4 1.4" /><path d="M14 10.5a4 4 0 0 0-5.7 0l-2.8 2.8a4 4 0 0 0 5.7 5.7l1.4-1.4" /></>)
export const IconSearch = make(<><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>)
export const IconPlus = make(<path d="M12 5v14M5 12h14" />)
export const IconCopy = make(<><rect x="9" y="9" width="12" height="12" rx="2.5" /><path d="M15 6.5V5.5A2.5 2.5 0 0 0 12.5 3H5.5A2.5 2.5 0 0 0 3 5.5v7A2.5 2.5 0 0 0 5.5 15h1" /></>)
export const IconArrowRight = make(<path d="M4 12h15m-6-6 6 6-6 6" />)
export const IconChevronRight = make(<path d="m9 5 7 7-7 7" />)
export const IconTrendUp = make(<path d="m5 15 5-5 3.5 3.5L19 7m0 0h-4.5M19 7v4.5" />)
export const IconTrendDown = make(<path d="m5 9 5 5 3.5-3.5L19 17m0 0h-4.5M19 17v-4.5" />)
export const IconCalendar = make(<><rect x="3.5" y="5" width="17" height="16" rx="3" /><path d="M3.5 10h17M8 3v4M16 3v4" /></>)
export const IconWallet = make(<><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H18v3" /><rect x="3" y="7.5" width="18" height="12.5" rx="3" /><circle cx="16.5" cy="14" r="1.2" fill="currentColor" stroke="none" /></>)
export const IconCoins = make(<><ellipse cx="12" cy="6.5" rx="7.5" ry="3.5" /><path d="M4.5 6.5v5c0 1.9 3.4 3.5 7.5 3.5s7.5-1.6 7.5-3.5v-5" /><path d="M4.5 11.5v5c0 1.9 3.4 3.5 7.5 3.5s7.5-1.6 7.5-3.5v-5" /></>)
export const IconUser = make(<><circle cx="12" cy="8" r="3.6" /><path d="M4.5 20c0-3.6 3.4-5.6 7.5-5.6s7.5 2 7.5 5.6" /></>)
export const IconUsers = make(<><circle cx="9.5" cy="8.5" r="3.4" /><path d="M3 20c0-3.4 3-5.3 6.5-5.3S16 16.6 16 20" /><path d="M16.5 5.6a3.4 3.4 0 0 1 0 6.6M18 14.9c2 .6 3.5 2.2 3.5 5.1" /></>)
export const IconSettings = make(<><circle cx="12" cy="12" r="3" /><path d="M19.4 14.5a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1v.3a2 2 0 1 1-4 0v-.2a1.6 1.6 0 0 0-2.8-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0-1.1-2.7h-.3a2 2 0 1 1 0-4h.2A1.6 1.6 0 0 0 4.6 7l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 2.7-1.1v-.3a2 2 0 1 1 4 0v.2a1.6 1.6 0 0 0 2.8 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0 1.1 2.7h.3a2 2 0 1 1 0 4h-.2a1.6 1.6 0 0 0-1.5 1Z" /></>)
export const IconActivity = make(<path d="M3 12h4l2.5-7 5 14L17 12h4" />)
export const IconBook = make(<><path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H20v16H6.5A2.5 2.5 0 0 0 4 20.5Z" /><path d="M4 20.5A2.5 2.5 0 0 1 6.5 18H20v4H6.5A2.5 2.5 0 0 1 4 19.5Z" /></>)
export const IconCode = make(<path d="m8.5 8-4 4 4 4M15.5 8l4 4-4 4M13.5 5l-3 14" />)
export const IconChat = make(<path d="M20.5 12a7.5 7.5 0 0 1-10.9 6.7L4.5 20l1.3-5A7.5 7.5 0 1 1 20.5 12Z" />)
export const IconBulb = make(<><path d="M9 17h6M10 20.5h4" /><path d="M12 3a6 6 0 0 0-3.5 10.9c.6.5.9 1.1 1 1.8l.1.3h4.8l.1-.3c.1-.7.4-1.3 1-1.8A6 6 0 0 0 12 3Z" /></>)
export const IconSend = make(<><path d="m21 3-9.5 9.5" /><path d="M21 3 14.5 21 11 13 3 9.5 21 3Z" /></>)
export const IconHeadset = make(<><path d="M4 14v-2a8 8 0 0 1 16 0v2" /><path d="M20 14.5v2a3 3 0 0 1-3 3h-3" /><rect x="2" y="13" width="4" height="6" rx="1.8" /><rect x="18" y="13" width="4" height="6" rx="1.8" /></>)
export const IconSparkles = make(<><path d="m12 3 1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6L12 3Z" /><path d="M18.5 15.5 19.3 18l2.2.8-2.2.8-.8 2.4-.8-2.4-2.2-.8 2.2-.8.8-2.5Z" /></>)
export const IconDots = make(<><circle cx="5" cy="12" r="1.4" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" /><circle cx="19" cy="12" r="1.4" fill="currentColor" stroke="none" /></>)
export const IconLogout = make(<><path d="M14 4.5h4A1.5 1.5 0 0 1 19.5 6v12a1.5 1.5 0 0 1-1.5 1.5h-4" /><path d="M10 8.5 13.5 12 10 15.5M13.5 12H4" /></>)
export const IconCheck = make(<path d="m5 12.5 4.5 4.5L19 7" />)
