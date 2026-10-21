// Code samples for the docs and landing page. `base` is the API origin (without /v1), `key` is the API key, `model` is the model name.
export function snippets(base, key, model) {
  return [
    {
      name: 'curl',
      code: `curl ${base}/v1/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "Salom! Bir jumlada tanishtir."}]
  }'`,
    },
    {
      name: 'Python',
      code: `from openai import OpenAI

client = OpenAI(base_url="${base}/v1", api_key="${key}")

resp = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "Salom! Bir jumlada tanishtir."}],
)
print(resp.choices[0].message.content)
print(resp.usage)  # kirish va chiqish tokenlari`,
    },
    {
      name: 'JavaScript',
      code: `import OpenAI from "openai";

const client = new OpenAI({ baseURL: "${base}/v1", apiKey: "${key}" });

const resp = await client.chat.completions.create({
  model: "${model}",
  messages: [{ role: "user", content: "Salom! Bir jumlada tanishtir." }],
});
console.log(resp.choices[0].message.content);`,
    },
  ]
}
