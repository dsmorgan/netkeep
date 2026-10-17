import { createFileRoute } from '@tanstack/react-router'

import { NotFound } from '@/components/not-found'
import { ContactDetailPage } from '@/features/contacts/contact-detail-page'

export const Route = createFileRoute('/contacts/$contactId')({
  component: ContactDetailRoute,
})

function ContactDetailRoute() {
  const { contactId } = Route.useParams()
  const id = Number(contactId)
  if (!Number.isSafeInteger(id) || id <= 0) {
    return (
      <NotFound to="/contacts" backLabel="Back to contacts">
        There is no contact at this address.
      </NotFound>
    )
  }
  return <ContactDetailPage contactId={id} />
}
