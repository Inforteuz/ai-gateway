import test from 'node:test'
import assert from 'node:assert/strict'
import uz from '../src/i18n/uz.js'
import en from '../src/i18n/en.js'
import ru from '../src/i18n/ru.js'
import uzExtra from '../src/i18n/uz-extra.js'
import enExtra from '../src/i18n/en-extra.js'
import ruExtra from '../src/i18n/ru-extra.js'
const placeholders = s => [...String(s).matchAll(/\{(\w+)\}/g)].map(m => m[1]).sort()
for (const [name, dicts] of [['main', { uz, en, ru }], ['extended', { uz: uzExtra, en: enExtra, ru: ruExtra }]]) {
  test(`${name}: all three languages have complete keys and placeholders`, () => {
    const keys = Object.keys(dicts.uz).sort()
    for (const lang of ['en', 'ru']) {
      assert.deepEqual(Object.keys(dicts[lang]).sort(), keys)
      for (const key of keys) {
        assert.equal(typeof dicts[lang][key], 'string', `${lang}: ${key}`)
        assert.ok(dicts[lang][key].trim(), `${lang}: empty ${key}`)
        assert.deepEqual(placeholders(dicts[lang][key]), placeholders(dicts.uz[key]), `${lang}: ${key}`)
      }
    }
  })
}
