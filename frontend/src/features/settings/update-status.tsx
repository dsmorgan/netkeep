import { useQuery } from '@tanstack/react-query'

import { updateCheckQuery } from './api'
import { CopyValue } from './copy-button'

/**
 * What `serve`'s daily update check found (#762): a newer release and the command to run,
 * or when it last checked. It installs nothing (ADR 0011 rule 9).
 */
export function UpdateStatus() {
  const check = useQuery(updateCheckQuery)
  if (check.isPending) return null
  if (check.isError) {
    return <p role="alert">Couldn't read the update check: {check.error.message}</p>
  }
  const { enabled, current, checked_at, ok, available, command } = check.data
  if (!enabled) {
    return (
      <p role="status">
        This is netkeep {current}. The daily check is off, so netkeep doesn't ask GitHub. To check
        by hand, run <code>netkeep update --check</code>.
      </p>
    )
  }
  if (available !== null && command !== null) {
    return (
      <div role="status" className="space-y-2">
        <p className="font-medium">
          netkeep {available} is available; this is {current}.
        </p>
        <CopyValue label="Update command" value={command} />
      </div>
    )
  }
  if (checked_at === null) {
    return (
      <p role="status">
        This is netkeep {current}. netkeep serve hasn't checked yet: it checks a few minutes after
        it starts.
      </p>
    )
  }
  const when = new Date(checked_at).toLocaleString()
  return ok === false ? (
    <p role="status">
      This is netkeep {current}. The last check, {when}, got no answer; netkeep tries again a day
      later.
    </p>
  ) : (
    <p role="status">
      netkeep {current} is up to date (checked {when}).
    </p>
  )
}
