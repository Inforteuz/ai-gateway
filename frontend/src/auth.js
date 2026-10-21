import { createContext, useContext } from 'react'
import { normalizeSite } from './site.js'

export const AuthCtx = createContext(null)
export const useAuth = () => useContext(AuthCtx)

const FALLBACK_SITE = normalizeSite(null)
/** Platform name and URLs (`site`), with defaults applied. */
export const useSite = () => useContext(AuthCtx)?.site || FALLBACK_SITE
