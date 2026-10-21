import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

import { metricsQuery, type DashboardMetrics, type MetricsWindow } from './api'
import {
  SPARK_HEIGHT,
  SPARK_WIDTH,
  TILES,
  WINDOWS,
  isEmpty,
  rangeText,
  readWindow,
  rememberWindow,
  sparkPoints,
  type TileDef,
} from './metrics'

const HEADING_ID = 'metrics-heading'

/**
 * The metric tiles (#740): Sent, Replies, Bounced, Changed jobs and New contacts over
 * the last 7 days, 30 days or 12 months, each with a sparkline. The window is
 * remembered per browser. Nothing compares with an earlier period.
 */
export function MetricTiles() {
  const [period, setPeriod] = useState<MetricsWindow>(readWindow)
  const metrics = useQuery(metricsQuery(period))
  const heading = WINDOWS.find((choice) => choice.value === period)?.heading ?? ''

  function choose(value: MetricsWindow) {
    setPeriod(value)
    rememberWindow(value)
  }

  let body
  if (metrics.isPending) {
    body = (
      <p role="status" className="text-sm text-muted-foreground">
        Counting…
      </p>
    )
  } else if (metrics.isError) {
    body = (
      <Card size="sm" tone="bad" className="max-w-xl" role="alert">
        <CardHeader>
          <CardTitle level={3}>The numbers couldn’t be read</CardTitle>
        </CardHeader>
        <CardContent>
          <p>{metrics.error.message}</p>
        </CardContent>
      </Card>
    )
  } else {
    const data = metrics.data
    body = (
      <>
        {isEmpty(data) && !metrics.isPlaceholderData && (
          <p className="text-sm text-muted-foreground">
            Nothing yet in this window. The numbers fill in as netkeep sends, syncs and imports.
          </p>
        )}
        <ul
          aria-label="Metrics"
          aria-busy={metrics.isPlaceholderData}
          className={cn(
            'grid grid-cols-[repeat(auto-fit,minmax(min(100%,160px),1fr))] gap-3 transition-opacity',
            metrics.isPlaceholderData && 'opacity-60',
          )}
        >
          {TILES.map((tile) => (
            <Tile key={tile.key} tile={tile} metrics={data} />
          ))}
        </ul>
      </>
    )
  }

  return (
    <section aria-labelledby={HEADING_ID} data-region="metrics" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h2 id={HEADING_ID} className="font-heading text-card-title font-bold tracking-heading">
          {heading}
        </h2>
        <div
          role="group"
          aria-label="Period"
          className="flex items-center gap-1 rounded-lg bg-muted/50 p-1"
        >
          {WINDOWS.map((choice) => (
            <Button
              key={choice.value}
              size="sm"
              variant={period === choice.value ? 'secondary' : 'ghost'}
              aria-pressed={period === choice.value}
              onClick={() => choose(choice.value)}
            >
              {choice.label}
            </Button>
          ))}
        </div>
      </div>
      {body}
    </section>
  )
}

function Tile({ tile, metrics }: { tile: TileDef; metrics: DashboardMetrics }) {
  const suffix = tile.suffix?.(metrics) ?? null
  const detail = tile.detail?.(metrics)
  return (
    <li data-tile={tile.key} className="nk-glass flex flex-col gap-0.5 px-4 pt-3.5 pb-2.5">
      <span className="nk-eyebrow">{tile.label}</span>
      <span className="text-[1.875rem] leading-tight font-extrabold tracking-tight tabular-nums">
        {tile.total(metrics).toLocaleString()}
        {suffix !== null && (
          <small className="ml-1.5 text-sm font-semibold tracking-normal text-muted-foreground">
            {/* A space, so a screen reader doesn't read "4" and "22%" as "422%". */} {suffix}
            {tile.suffixName !== undefined && <span className="sr-only"> {tile.suffixName}</span>}
          </small>
        )}
      </span>
      <Sparkline values={metrics.series.map(tile.point)} />
      {/* Each part keeps to one line; in a narrow tile the detail moves under the dates. */}
      <span className="flex flex-wrap justify-between gap-x-2 text-[11.5px] text-muted-foreground">
        <span className="whitespace-nowrap">
          {rangeText(metrics.start, metrics.end, metrics.window)}
        </span>
        {detail !== undefined && <span className="whitespace-nowrap">{detail}</span>}
      </span>
    </li>
  )
}

/**
 * A sparkline: the area under the series and its line, stretched to the tile's width,
 * and a dot on the last value. The dot is a positioned element, not an SVG circle, so
 * the stretch doesn't squash it.
 */
function Sparkline({ values }: { values: number[] }) {
  const spark = sparkPoints(values)
  if (spark === null) return null
  return (
    <span aria-hidden="true" className="relative mt-1.5 block h-7.5" data-testid="sparkline">
      <svg
        viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
        preserveAspectRatio="none"
        className="block size-full overflow-visible"
      >
        <line
          x1="0"
          x2={SPARK_WIDTH}
          y1={SPARK_HEIGHT - 1}
          y2={SPARK_HEIGHT - 1}
          className="stroke-border"
          vectorEffect="non-scaling-stroke"
        />
        <polygon points={spark.area} className="fill-primary/18" />
        <polyline
          points={spark.line}
          fill="none"
          strokeWidth={1.6}
          strokeLinejoin="round"
          strokeLinecap="round"
          className="stroke-primary"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <span
        className="absolute size-1.5 -translate-1/2 rounded-full bg-primary ring-2 ring-card"
        style={{ left: `${spark.end.x * 100}%`, top: `${spark.end.y * 100}%` }}
      />
    </span>
  )
}
