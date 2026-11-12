import { useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { isVideoModel } from '../site.js'
import { api } from '../api.js'
import { useAuth } from '../auth.js'
import DemoReceipt from './DemoReceipt.jsx'
import { useLoad } from './ui.jsx'

// Left panel of the sign-in pages: the product's own object, a receipt that prints
// what one request costs (same component and same model choice as the landing hero).
// Decorative here, so hidden from screen readers; nothing shows until prices load.
export default function AuthArt() {
  useLocaleRefresh()
  const { freeMode } = useAuth()
  const { data } = useLoad(() => api.get('/portal/models'), [])
  const list = data?.items || []
  const m = list.find((x) => !isVideoModel(x) && (x.price_in_per_1m || x.price_out_per_1m)) || list[0]
  return (
    <div className="auth-art" aria-hidden="true">
      {m && (
        <div className="auth-receipt">
          <DemoReceipt priceIn={m.price_in_per_1m} priceOut={m.price_out_per_1m} model={m.id} free={freeMode} />
        </div>
      )}
    </div>
  )
}
