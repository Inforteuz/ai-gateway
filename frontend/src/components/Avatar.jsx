import { useState } from 'react'

/**
 * The user's profile photo, or the first letter of their name when there is none
 * (or the picture fails to load). `user.avatar` is the photo's version: it goes into
 * the URL, so a new upload is fetched at once while an unchanged one stays cached.
 */
export default function UserAvatar({ user, className = 'avatar' }) {
  const [broken, setBroken] = useState(null)
  const letter = ((user?.name || user?.email || '?').trim().slice(0, 1) || '?').toUpperCase()
  const ver = user?.avatar
  if (ver && broken !== ver) {
    return (
      <span className={`${className} avatar--photo`} aria-hidden="true">
        <img src={`/portal/account/avatar?v=${ver}`} alt="" onError={() => setBroken(ver)} />
      </span>
    )
  }
  return <span className={className} aria-hidden="true">{letter}</span>
}

const SIDE = 256

/** Crops a picture file to its centre square and returns a 256 px JPEG data URL. */
export function squareJpeg(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      const s = Math.min(img.naturalWidth, img.naturalHeight)
      const canvas = document.createElement('canvas')
      canvas.width = SIDE
      canvas.height = SIDE
      const ctx = canvas.getContext('2d')
      ctx.fillStyle = '#ffffff'           // transparent PNGs get a white background in JPEG
      ctx.fillRect(0, 0, SIDE, SIDE)
      ctx.drawImage(img, (img.naturalWidth - s) / 2, (img.naturalHeight - s) / 2, s, s, 0, 0, SIDE, SIDE)
      URL.revokeObjectURL(url)
      resolve(canvas.toDataURL('image/jpeg', 0.88))
    }
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('decode')) }
    img.src = url
  })
}
