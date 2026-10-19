/**
 * Record a response that came outside netkeep (#512): a call, a text, an email from
 * another address, another app, or in person. Neither poll sees one, so you record it,
 * and it ends the enrollments it answers as a detected reply does: no later step sends.
 *
 * By default it ends every enrollment of the contact that could still send (pending,
 * active or paused); you may pick one campaign instead. A completed enrollment may be
 * picked too, to count a late answer in its results. It shows on the contact's timeline
 * and, already handled, in the inbox.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { MessageSquareReply } from 'lucide-react'
import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input, Textarea } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { contactsKeys } from '@/features/contacts/api'
import { ErrorNote } from '@/features/crm/controls'
import { inboxKeys } from '@/features/inbox/api'

import {
  campaignKeys,
  recordOutsideResponse,
  type OutsideResponse,
  type ResponseOutcome,
  type ResponseVia,
} from './api'
import { fromLocalInput, toLocalInput } from './format'
import {
  OUTCOME_LABELS,
  STOPPABLE,
  VIA_LABELS,
  canRecordOn,
  type ResponseTarget,
} from './response-labels'

export function RecordResponseButton({
  contactId,
  contactName,
  targets,
  preferred,
  complete = false,
  size = 'sm',
}: {
  contactId: number
  contactName: string
  /** The enrollments you may pick instead of "every one that could still send". */
  targets: ResponseTarget[]
  /** Whether `targets` is every enrollment of the contact (the contact page), so the
   *  form can count the ones that could still send; an enrollment row knows only its own. */
  complete?: boolean
  /** The enrollment the button sits on, if any: picked by default when it is completed. */
  preferred?: number
  size?: 'sm' | 'xs'
}) {
  const [open, setOpen] = useState(false)
  // A fresh form each time the dialog opens: the key remounts it.
  const [session, setSession] = useState(0)
  const pickable = targets.filter((t) => canRecordOn(t.status))
  if (pickable.length === 0) return null
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (next) setSession((current) => current + 1)
      }}
    >
      <DialogTrigger
        render={
          <Button size={size} variant="outline">
            <MessageSquareReply data-icon="inline-start" aria-hidden />
            Record a response
          </Button>
        }
      />
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Record a response from {contactName || 'this contact'}</DialogTitle>
          <DialogDescription>
            For a response netkeep cannot see: a call, a text, in person, or another app. It ends
            their campaigns as a reply does, so no later step sends.
          </DialogDescription>
        </DialogHeader>
        <RecordResponseForm
          key={session}
          contactId={contactId}
          targets={pickable}
          preferred={preferred}
          complete={complete}
          onDone={() => setOpen(false)}
        />
      </DialogContent>
    </Dialog>
  )
}

function endedText(answer: OutsideResponse): string {
  const n = answer.enrollments.length
  return `Recorded. ${n === 1 ? '1 enrollment' : `${n} enrollments`} ended or counted the reply.`
}

function RecordResponseForm({
  contactId,
  targets,
  preferred,
  complete,
  onDone,
}: {
  contactId: number
  targets: ResponseTarget[]
  preferred?: number
  complete: boolean
  onDone: () => void
}) {
  const id = useId()
  const queryClient = useQueryClient()
  const stoppable = targets.filter((t) => STOPPABLE.has(t.status))
  // On an enrollment row the contact may have others that could still send.
  const offerAll = !complete || stoppable.length > 0
  const preferredTarget = targets.find((t) => t.id === preferred)
  const [via, setVia] = useState<ResponseVia>('call')
  const [at, setAt] = useState(() => toLocalInput(new Date().toISOString()))
  const [scope, setScope] = useState<string>(
    !offerAll || preferredTarget?.status === 'completed'
      ? String(preferredTarget?.id ?? targets[0]?.id ?? '')
      : '',
  )
  const [outcome, setOutcome] = useState<ResponseOutcome | ''>('')
  const [doNotContact, setDoNotContact] = useState(true)
  const [note, setNote] = useState('')
  const [done, setDone] = useState<string | null>(null)

  // The form is new each time the dialog opens, so its opening time stands for now.
  const [openedAt] = useState(() => Date.now())
  const when = fromLocalInput(at)
  const future = when !== null && new Date(when).getTime() > openedAt + 5 * 60_000
  const save = useMutation({
    mutationFn: () =>
      recordOutsideResponse(contactId, {
        via,
        at: when as string,
        note: note.trim() === '' ? null : note.trim(),
        outcome: outcome === '' ? null : outcome,
        do_not_contact: outcome === 'opt_out' && doNotContact,
        enrollment_id: scope === '' ? null : Number(scope),
      }),
    onSuccess: (answer) => {
      setDone(endedText(answer))
      void queryClient.invalidateQueries({ queryKey: campaignKeys.all })
      void queryClient.invalidateQueries({ queryKey: inboxKeys.all })
      void queryClient.invalidateQueries({ queryKey: contactsKeys.detail(contactId) })
      void queryClient.invalidateQueries({ queryKey: contactsKeys.timeline(contactId) })
    },
  })

  if (done !== null) {
    return (
      <div className="grid gap-3">
        <p role="status">{done}</p>
        <DialogFooter>
          <Button onClick={onDone}>Close</Button>
        </DialogFooter>
      </div>
    )
  }

  return (
    <form
      className="grid gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (when !== null && !future) save.mutate()
      }}
    >
      <div className="grid gap-1">
        <Label htmlFor={`${id}-via`}>How they responded</Label>
        <Select
          id={`${id}-via`}
          className="w-full"
          value={via}
          onChange={(event) => setVia(event.target.value as ResponseVia)}
        >
          {(Object.keys(VIA_LABELS) as ResponseVia[]).map((v) => (
            <option key={v} value={v}>
              {VIA_LABELS[v]}
            </option>
          ))}
        </Select>
      </div>
      <div className="grid gap-1">
        <Label htmlFor={`${id}-at`}>When</Label>
        <Input
          id={`${id}-at`}
          type="datetime-local"
          value={at}
          onChange={(event) => setAt(event.target.value)}
          aria-invalid={future}
          className="w-fit"
        />
        {future && <p className="text-xs text-destructive">A response cannot be in the future.</p>}
      </div>
      <div className="grid gap-1">
        <Label htmlFor={`${id}-scope`}>What it ends</Label>
        <Select
          id={`${id}-scope`}
          className="w-full"
          value={scope}
          onChange={(event) => setScope(event.target.value)}
        >
          {offerAll && (
            <option value="">
              {!complete
                ? 'Every campaign of theirs that could still send'
                : stoppable.length === 1
                  ? 'Their campaign that could still send'
                  : `All ${stoppable.length} of their campaigns that could still send`}
            </option>
          )}
          {targets.map((t) => (
            <option key={t.id} value={String(t.id)}>
              Only {t.campaignName}
              {t.status === 'completed' ? ' (completed: counts the reply)' : ''}
            </option>
          ))}
        </Select>
      </div>
      <div className="grid gap-1">
        <Label htmlFor={`${id}-outcome`}>What they said (optional)</Label>
        <Select
          id={`${id}-outcome`}
          className="w-full"
          value={outcome}
          onChange={(event) => setOutcome(event.target.value as ResponseOutcome | '')}
        >
          <option value="">Not recorded</option>
          {(Object.keys(OUTCOME_LABELS) as ResponseOutcome[]).map((o) => (
            <option key={o} value={o}>
              {OUTCOME_LABELS[o]}
            </option>
          ))}
        </Select>
        {outcome === 'opt_out' && (
          <Label className="font-normal">
            <Checkbox
              checked={doNotContact}
              onCheckedChange={(checked) => setDoNotContact(checked === true)}
            />
            Also mark them do not contact, and put their addresses on the do-not-send list
          </Label>
        )}
      </div>
      <div className="grid gap-1">
        <Label htmlFor={`${id}-note`}>Note (optional)</Label>
        <Textarea
          id={`${id}-note`}
          value={note}
          maxLength={500}
          rows={3}
          onChange={(event) => setNote(event.target.value)}
        />
      </div>
      <p className="text-muted-foreground">
        {outcome === 'opt_out' ? 'They end as opted out' : 'They end as replied'}. It shows on the
        timeline and, already handled, in the inbox.
      </p>
      {save.isError && <ErrorNote label="Could not record the response." error={save.error} />}
      <DialogFooter>
        <DialogClose render={<Button variant="outline">Cancel</Button>} />
        <Button type="submit" disabled={when === null || future || save.isPending}>
          {save.isPending ? 'Recording…' : 'Record the response'}
        </Button>
      </DialogFooter>
    </form>
  )
}
