import type { SitlLocation } from '@/types/sitl'

// One logged row of a Simple Ping Survey CSV, in the units the rest of this module works in.
export interface SurveyRow {
  latitude: number
  longitude: number
  // Metres below the transducer.
  depth: number
  confidence: number
  heading: number
}

// Every format Simple Ping Survey has written names the same things a little differently: the
// legacy 8-column logs say Distance and Yaw where the current ones say Depth and Vessel heading,
// and the newer columns sit between the old ones. Finding them by name reads all of them.
const COLUMNS = {
  depth: ['Depth (cm)', 'Distance (cm)'],
  confidence: ['Confidence (%)'],
  latitude: ['Latitude'],
  longitude: ['Longitude'],
  heading: ['Vessel heading (deg)', 'Yaw (deg)'],
} as const

type Column = keyof typeof COLUMNS

/** Read a Simple Ping Survey log, any version of it, into rows. */
export function parseSurvey(text: string): SurveyRow[] {
  const lines = text.split(/\r?\n/).filter((line) => line.trim() !== '')
  if (lines.length < 2) {
    throw new Error('The file has no survey rows in it.')
  }
  const header = lines[0].split(',').map((name) => name.trim())
  const index = {} as Record<Column, number>
  const missing: string[] = []
  for (const [column, names] of Object.entries(COLUMNS) as [Column, readonly string[]][]) {
    index[column] = header.findIndex((name) => names.includes(name))
    if (index[column] < 0 && column !== 'heading') {
      missing.push(names.join(' or '))
    }
  }
  if (missing.length) {
    throw new Error(`This is not a Simple Ping Survey log: it has no ${missing.join(', ')} column.`)
  }

  const rows: SurveyRow[] = []
  for (const line of lines.slice(1)) {
    const cells = line.split(',')
    const value = (column: Column): number => (index[column] < 0 ? 0 : Number(cells[index[column]]))
    const row = {
      latitude: value('latitude'),
      longitude: value('longitude'),
      depth: value('depth') / 100,
      confidence: value('confidence'),
      heading: value('heading'),
    }
    if (Object.values(row).every(Number.isFinite)) {
      rows.push(row)
    }
  }
  return rows
}

export interface Soundings {
  rows: SurveyRow[]
  // Rows left out: too unsure of the bottom, no bottom at all, or logged before a position fix.
  dropped: number
  // Where the survey started, which is where the vehicle can be spawned to run it again.
  start: SitlLocation | null
}

/** The rows a seafloor can be built from: a bottom the Ping was sure of, at a known position. */
export function selectSoundings(rows: SurveyRow[], minConfidence: number): Soundings {
  const kept = rows.filter(
    (row) =>
      row.confidence >= minConfidence &&
      row.depth > 0 &&
      !(row.latitude === 0 && row.longitude === 0) &&
      Math.abs(row.latitude) <= 90 &&
      Math.abs(row.longitude) <= 180,
  )
  const first = kept[0]
  return {
    rows: kept,
    dropped: rows.length - kept.length,
    start: first
      ? { latitude: first.latitude, longitude: first.longitude, altitude: 0, heading: ((first.heading % 360) + 360) % 360 }
      : null,
  }
}
