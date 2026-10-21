import { useQuery } from '@tanstack/react-query'

import { meQuery } from '@/api/queries'
import { SectionAnchor } from '@/components/layout/section-anchor'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

import { AboutYouSection } from './about-you-section'
import { BackupsSection } from './backups-section'
import { LoginItemSection } from './login-item-section'
import { ConfigSections } from './config-section'
import { DoNotSendSection } from './do-not-send-section'
import { type GmailOutcome, GmailSection } from './gmail-section'
import { PostureSection } from './posture-section'
import { SendingHoursSection } from './sending-hours-section'
import { SupportSection } from './support-section'

/**
 * Settings, one card per section. Each section has an anchor a link can name, such as
 * `/settings#gmail` (#739); the config groups take theirs from `GROUPS`.
 */
export function SettingsPage({ gmail = {} }: { gmail?: GmailOutcome }) {
  const me = useQuery(meQuery)

  return (
    <div className="grid max-w-3xl gap-4">
      <Card size="sm">
        <CardHeader>
          <CardTitle level={2}>Settings</CardTitle>
          <CardDescription>
            Gmail auth, sending hours, about you, LinkedIn budgets, hours and runs, campaign
            defaults, backups, and a support bundle to send when something goes wrong. You change
            them here; config.toml is optional, and a value it sets wins.
          </CardDescription>
          <p className="text-sm text-muted-foreground">
            netkeep’s UI requires Google Chrome. Other browsers may work, but they are not tested or
            supported.
          </p>
        </CardHeader>
      </Card>

      <SectionAnchor id="gmail">
        <GmailSection outcome={gmail} />
      </SectionAnchor>

      <SectionAnchor id="sending-hours">
        <SendingHoursSection />
      </SectionAnchor>

      <SectionAnchor id="about-you">
        <AboutYouSection />
      </SectionAnchor>

      <ConfigSections />

      <SectionAnchor id="backups">
        <BackupsSection />
      </SectionAnchor>

      <SectionAnchor id="login-item">
        <LoginItemSection />
      </SectionAnchor>

      <SectionAnchor id="do-not-send">
        <DoNotSendSection />
      </SectionAnchor>

      <SectionAnchor id="posture">
        <PostureSection />
      </SectionAnchor>

      <SectionAnchor id="support">
        <SupportSection />
      </SectionAnchor>

      <Card size="sm">
        <CardHeader>
          <CardTitle level={2}>Current user</CardTitle>
          <CardDescription>
            Raw <code className="font-mono">GET /api/v1/me</code> payload.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {me.isPending && <p className="text-muted-foreground">Loading…</p>}
          {me.isError && <p className="text-muted-foreground">Backend unreachable.</p>}
          {me.isSuccess && (
            <pre className="overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs">
              {JSON.stringify(me.data, null, 2)}
            </pre>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
