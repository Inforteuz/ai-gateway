import { Suspense } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { BrandLoader } from './ui.jsx'

/** The routed page, with a loader while its code arrives and a short entrance.
 *
 *  The Suspense boundary sits HERE, inside the layout, not around the whole app.
 *  Pages are loaded on demand, and with the only boundary at the top, moving to a
 *  page whose code was not loaded yet suspended everything: the sidebar and top
 *  bar vanished and the screen went blank down to a lone "…". Now the frame stays
 *  and only the content area waits.
 *
 *  The wrapper is keyed by the path, so each page mounts fresh and plays the
 *  entrance (`.page-enter`); a query-string change on the same page does not. */
export default function RouteOutlet() {
  const { pathname } = useLocation()
  return (
    <Suspense fallback={<BrandLoader />}>
      <div key={pathname} className="page-enter"><Outlet /></div>
    </Suspense>
  )
}
