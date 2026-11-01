import { vi } from 'vitest'

type FetchHandler = (request: Request) => Response | Promise<Response>

const offline: FetchHandler = () => {
  throw new TypeError('network disabled in tests')
}

let handler: FetchHandler = offline

/**
 * Raw content for a `File` a fixture built, keyed by the `File` itself.
 *
 * Only `RelativeRequest`'s hand-rolled multipart encoding (below) reads this;
 * see its comment for why it exists. A `File` nobody registered is sent as an
 * empty part rather than throwing, since most tests never inspect what an
 * upload's bytes actually were.
 */
const fileContents = new WeakMap<File, string>()

/** Opts a fixture's `File` into `RelativeRequest`'s multipart encoding, and hands it back. */
export function registerFileContent(file: File, content: string): File {
  fileContents.set(file, content)
  return file
}

/** A value as it goes inside a `"..."`-quoted multipart header parameter. */
function quotedParam(value: string): string {
  // What Node's own multipart writer and every browser do with a `"` or a
  // line break in a filename or field name, so a crafted fixture name can't
  // break out of the header line it's quoted into.
  return value.replace(/\r/g, '%0D').replace(/\n/g, '%0A').replace(/"/g, '%22')
}

/**
 * A `multipart/form-data` body, built by hand from a `FormData`'s own entries.
 *
 * Under vitest's jsdom environment, `fetch`/`Request` are Node's real
 * implementation, but `File`/`Blob`/`FormData` are jsdom's own polyfills —
 * mixing the two is exactly the split `RelativeRequest` already exists to
 * paper over for relative URLs. Handing a jsdom `FormData` straight to a real
 * `Request` throws deep inside Node's multipart serializer, which expects its
 * *own* `File`/`Blob` internals. A real browser has no such split: `fetch`,
 * `Request`, and `FormData` all come from the same implementation there, so
 * this only has to exist here. Reading a `File`'s bytes is always async,
 * which a constructor cannot await, so this reads `fileContents` instead —
 * populated by whichever fixture (`csvFile`, `zipFile`, …) built the `File` —
 * rather than the `File` itself.
 */
function encodeMultipart(form: FormData): { body: string; contentType: string } {
  const boundary = `----testformdata${Math.random().toString(16).slice(2)}`
  const parts: string[] = []
  for (const [name, value] of form.entries()) {
    if (typeof value === 'string') {
      parts.push(
        `--${boundary}\r\nContent-Disposition: form-data; name="${quotedParam(name)}"\r\n\r\n` +
          `${value}\r\n`,
      )
      continue
    }
    const filename = quotedParam(value.name)
    const type = value.type || 'application/octet-stream'
    const content = fileContents.get(value) ?? ''
    parts.push(
      `--${boundary}\r\nContent-Disposition: form-data; name="${quotedParam(name)}"; ` +
        `filename="${filename}"\r\nContent-Type: ${type}\r\n\r\n${content}\r\n`,
    )
  }
  parts.push(`--${boundary}--\r\n`)
  return { body: parts.join(''), contentType: `multipart/form-data; boundary=${boundary}` }
}

/** A `name="..."` parameter's value in a `Content-Disposition` line, or null. */
function dispositionParam(line: string, param: string): string | null {
  const match = new RegExp(`;\\s*${param}="([^"]*)"`).exec(line)
  return match?.[1] ?? null
}

/**
 * A fake backend's read of an upload: the `multipart/form-data` body that
 * `encodeMultipart` built, parsed back by hand into a jsdom `FormData`.
 *
 * Not `request.formData()`: Node 25's parser asserts that each part it makes
 * is its own `File`, and under jsdom it isn't, so every upload test failed
 * there while passing on Node 22 (#746 re-review, via #674). This reads only
 * what `encodeMultipart` writes, which is all a test's upload ever is.
 */
export async function readMultipart(request: Request): Promise<FormData> {
  const contentType = request.headers.get('content-type') ?? ''
  const boundary = /boundary=(.+)$/.exec(contentType)?.[1]
  if (boundary === undefined) throw new Error(`no multipart boundary in ${contentType}`)
  const text = await request.text()
  const form = new FormData()
  for (const part of text.split(`--${boundary}`).slice(1)) {
    if (part.startsWith('--')) break // the closing delimiter
    const split = part.indexOf('\r\n\r\n')
    if (split === -1) throw new Error('a multipart part with no blank line after its headers')
    const headers = part.slice(0, split).split('\r\n')
    const value = part.slice(split + 4).replace(/\r\n$/, '')
    const disposition = headers.find((h) => /^content-disposition:/i.test(h)) ?? ''
    const name = dispositionParam(disposition, 'name')
    if (name === null) throw new Error('a multipart part with no field name')
    const filename = dispositionParam(disposition, 'filename')
    if (filename === null) {
      form.append(name, value)
    } else {
      const type = headers.find((h) => /^content-type:/i.test(h))?.replace(/^[^:]*:\s*/, '')
      form.append(name, new File([value], filename, { type: type ?? '' }))
    }
  }
  return form
}

/**
 * Node's `Request` rejects relative URLs, but the app uses them: the client's
 * `baseUrl` is empty and a browser resolves against the page origin. Resolve
 * against jsdom's location instead so the same code runs under vitest.
 *
 * A `FormData` body is re-encoded by hand first (see `encodeMultipart`
 * above) — but only when nothing has already set a content-type. Real app
 * code never does (a browser has to pick the multipart boundary itself, so
 * setting one is a bug), so re-encoding only fires on the same path a real
 * upload takes. If it ever did, silently fixing the header here would hide
 * exactly the bug that matters most on this route; passing the untouched
 * `FormData` through instead means a wrong content-type fails the same way
 * jsdom's `FormData` failed against a real `Request` before this file
 * existed, which is a real failure rather than a quietly repaired one.
 */
class RelativeRequest extends Request {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    const url = typeof input === 'string' ? new URL(input, window.location.href) : input
    const headers = new Headers(init?.headers)
    if (init?.body instanceof FormData && !headers.has('content-type')) {
      const { body, contentType } = encodeMultipart(init.body)
      headers.set('content-type', contentType)
      super(url, { ...init, body, headers })
    } else {
      super(url, init)
    }
  }
}

/**
 * Installs an offline `fetch` and the relative-URL `Request`. Must run before
 * the API client module is imported: `openapi-fetch` binds both globals when
 * `createClient` runs, so the vitest setup file is the place.
 */
export function installOfflineFetch(): void {
  vi.stubGlobal('Request', RelativeRequest)
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve().then(() =>
      handler(input instanceof Request ? input : new RelativeRequest(input, init)),
    ),
  )
}

/** Routes every fetch in the current test to `next`. Reset after each test. */
export function mockFetch(next: FetchHandler): void {
  handler = next
}

export function resetFetch(): void {
  handler = offline
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}
