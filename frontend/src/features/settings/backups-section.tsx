import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'

import { backupsQuery, createBackup } from './api'
import { humanSize } from './backup-size'
import { CopyValue } from './copy-button'

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * The database's backups (P6-04): each with its date, size and the command that restores
 * it, and **Back up now**. Restoring stays in the terminal, because `netkeeper serve` has
 * to be stopped first; the nightly switch and how many to keep are in the Backups
 * settings above.
 */
export function BackupsSection() {
  const backups = useQuery(backupsQuery)
  const queryClient = useQueryClient()
  const create = useMutation({
    mutationFn: createBackup,
    onSuccess: (data) => queryClient.setQueryData(backupsQuery.queryKey, data),
  })

  return (
    <Card size="sm">
      <CardHeader>
        <h2 className="font-heading text-sm leading-snug font-medium">Backup files</h2>
        <CardDescription>
          Snapshots of the database, newest first. To restore one, stop netkeeper serve, then run
          its command in a terminal: it backs up the database in place first, so you can undo it.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {backups.isPending && <p role="status">Loading…</p>}
        {backups.isError && <p role="alert">{message(backups.error)}</p>}
        {backups.isSuccess && (
          <>
            <p className="text-muted-foreground">
              In <code className="font-mono text-xs break-all">{backups.data.directory}</code>
            </p>
            {!backups.data.available && (
              <p className="text-muted-foreground">
                This database is not a SQLite file, so netkeeper does not back it up. Use your
                database&apos;s own tools.
              </p>
            )}
            {backups.data.available && (
              <div className="space-y-1">
                <Button
                  size="sm"
                  disabled={create.isPending}
                  onClick={() => {
                    create.reset()
                    create.mutate()
                  }}
                >
                  {create.isPending ? 'Backing up…' : 'Back up now'}
                </Button>
                {create.isError && <p role="alert">{message(create.error)}</p>}
              </div>
            )}
            {backups.data.backups.length === 0 && (
              <p className="text-muted-foreground">No backups yet.</p>
            )}
            {backups.data.backups.length > 0 && (
              <ul className="divide-y divide-border/60" aria-label="Backups">
                {backups.data.backups.map((backup) => (
                  <li key={backup.name} className="space-y-1 py-2">
                    <p>
                      {new Date(backup.created_at).toLocaleString()}{' '}
                      <span className="text-muted-foreground">· {humanSize(backup.size)}</span>
                    </p>
                    <CopyValue label="Restore with" value={backup.restore_command} />
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
