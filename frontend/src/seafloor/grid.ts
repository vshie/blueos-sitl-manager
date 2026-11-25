import { Delaunay } from 'd3-delaunay'

import type { SurveyRow } from '@/seafloor/survey'

// The flat projection the backend samples the grid on, which the two have to share exactly:
// metres per degree of latitude, and of longitude that times cos(latitude) at the origin.
export const METERS_PER_DEGREE = 111_319.49

// A cell the survey never reached.
export const NO_DATA = 0xffff

// The backend's limit, which keeps a grid to 2 MB.
export const MAX_CELLS = 1_000_000

// Cell sizes the automatic choice picks from, finest first. Finer than half a metre is finer
// than any GPS fix the survey was logged against.
const CELL_SIZES = [0.5, 1, 2, 5, 10, 20, 50, 100]

export interface GridOptions {
  // Metres between cell centres; null picks the finest that fits.
  cellSize: number | null
  // Survey lines further apart than this are not joined up, so open water outside the survey
  // stays empty instead of being bridged with a guess.
  maxGap: number
}

export interface DepthGrid {
  // Centre of the south-west cell.
  originLatitude: number
  originLongitude: number
  cellSize: number
  rows: number
  cols: number
  // Centimetres, row by row from the south, NO_DATA where there is none.
  depths: Uint16Array
  depthMin: number
  depthMax: number
  // Cells holding a depth, out of rows * cols.
  filled: number
}

function cellsFor(eastSpan: number, northSpan: number, cellSize: number): number {
  return (Math.floor(eastSpan / cellSize) + 1) * (Math.floor(northSpan / cellSize) + 1)
}

/**
 * Turn survey soundings into a depth grid.
 *
 * A single-beam survey is depth along the tracks it ran and nothing between them, so the
 * soundings are joined up into triangles and each cell takes the depth of the triangle over it,
 * straight between the three soundings at its corners. Soundings are averaged into cells first:
 * a boat holding station logs the same fix over and over, and the average is one sounding.
 */
export function buildGrid(rows: SurveyRow[], options: GridOptions): DepthGrid {
  if (!rows.length) {
    throw new Error('There are no soundings to build a seafloor from.')
  }
  // Reduced rather than spread into Math.min, which a long survey would overflow the stack with.
  const originLatitude = rows.reduce((low, row) => Math.min(low, row.latitude), Infinity)
  const originLongitude = rows.reduce((low, row) => Math.min(low, row.longitude), Infinity)
  const metersPerDegreeLon = METERS_PER_DEGREE * Math.cos((originLatitude * Math.PI) / 180)
  const east = rows.map((row) => (row.longitude - originLongitude) * metersPerDegreeLon)
  const north = rows.map((row) => (row.latitude - originLatitude) * METERS_PER_DEGREE)
  const eastSpan = east.reduce((high, value) => Math.max(high, value), 0)
  const northSpan = north.reduce((high, value) => Math.max(high, value), 0)

  const cellSize = options.cellSize ?? CELL_SIZES.find((size) => cellsFor(eastSpan, northSpan, size) <= MAX_CELLS)
  if (cellSize === undefined || cellsFor(eastSpan, northSpan, cellSize) > MAX_CELLS) {
    throw new Error(`At that cell size the seafloor would have more than ${MAX_CELLS.toLocaleString()} cells; use larger cells.`)
  }
  const cols = Math.floor(eastSpan / cellSize) + 1
  const gridRows = Math.floor(northSpan / cellSize) + 1

  // Average the soundings that fall in each cell.
  const sums = new Map<number, { depth: number; count: number }>()
  rows.forEach((row, index) => {
    const cell = Math.round(north[index] / cellSize) * cols + Math.round(east[index] / cellSize)
    const sum = sums.get(cell) ?? { depth: 0, count: 0 }
    sum.depth += row.depth
    sum.count += 1
    sums.set(cell, sum)
  })
  const binned = [...sums.entries()].map(([cell, sum]) => ({
    col: cell % cols,
    row: Math.floor(cell / cols),
    depth: sum.depth / sum.count,
  }))

  const depths = new Float32Array(cols * gridRows).fill(NaN)
  for (const point of binned) {
    depths[point.row * cols + point.col] = point.depth
  }

  // Between the tracks, linearly across each triangle the binned soundings make, in cell units.
  const delaunay = Delaunay.from(
    binned,
    (point) => point.col,
    (point) => point.row,
  )
  const maxGapCells = options.maxGap / cellSize
  const { triangles } = delaunay
  for (let t = 0; t < triangles.length; t += 3) {
    const a = binned[triangles[t]]
    const b = binned[triangles[t + 1]]
    const c = binned[triangles[t + 2]]
    const longest = Math.max(
      Math.hypot(a.col - b.col, a.row - b.row),
      Math.hypot(b.col - c.col, b.row - c.row),
      Math.hypot(c.col - a.col, c.row - a.row),
    )
    if (longest > maxGapCells) {
      continue
    }
    const area = (b.col - a.col) * (c.row - a.row) - (c.col - a.col) * (b.row - a.row)
    if (area === 0) {
      continue
    }
    const colLow = Math.max(0, Math.floor(Math.min(a.col, b.col, c.col)))
    const colHigh = Math.min(cols - 1, Math.ceil(Math.max(a.col, b.col, c.col)))
    const rowLow = Math.max(0, Math.floor(Math.min(a.row, b.row, c.row)))
    const rowHigh = Math.min(gridRows - 1, Math.ceil(Math.max(a.row, b.row, c.row)))
    for (let row = rowLow; row <= rowHigh; row++) {
      for (let col = colLow; col <= colHigh; col++) {
        const wa = ((b.col - col) * (c.row - row) - (c.col - col) * (b.row - row)) / area
        const wb = ((c.col - col) * (a.row - row) - (a.col - col) * (c.row - row)) / area
        const wc = 1 - wa - wb
        if (wa < -1e-9 || wb < -1e-9 || wc < -1e-9) {
          continue
        }
        const index = row * cols + col
        if (Number.isNaN(depths[index])) {
          depths[index] = wa * a.depth + wb * b.depth + wc * c.depth
        }
      }
    }
  }

  const encoded = new Uint16Array(depths.length)
  let depthMin = Infinity
  let depthMax = -Infinity
  let filled = 0
  depths.forEach((depth, index) => {
    if (Number.isNaN(depth)) {
      encoded[index] = NO_DATA
      return
    }
    encoded[index] = Math.min(Math.max(Math.round(depth * 100), 0), NO_DATA - 1)
    depthMin = Math.min(depthMin, depth)
    depthMax = Math.max(depthMax, depth)
    filled += 1
  })

  return {
    originLatitude,
    originLongitude,
    cellSize,
    rows: gridRows,
    cols,
    depths: encoded,
    depthMin,
    depthMax,
    filled,
  }
}

/** South-west and north-east corners of a grid's outer cell edges, for placing it on a map. */
export function gridBounds(grid: {
  originLatitude: number
  originLongitude: number
  cellSize: number
  rows: number
  cols: number
}): [[number, number], [number, number]] {
  const metersPerDegreeLon = METERS_PER_DEGREE * Math.cos((grid.originLatitude * Math.PI) / 180)
  const half = grid.cellSize / 2
  return [
    [grid.originLatitude - half / METERS_PER_DEGREE, grid.originLongitude - half / metersPerDegreeLon],
    [
      grid.originLatitude + (grid.rows * grid.cellSize - half) / METERS_PER_DEGREE,
      grid.originLongitude + (grid.cols * grid.cellSize - half) / metersPerDegreeLon,
    ],
  ]
}

/** The grid's bytes as base64, little-endian as the backend reads them. */
export function encodeGrid(depths: Uint16Array): string {
  const bytes = new Uint8Array(depths.length * 2)
  const view = new DataView(bytes.buffer)
  depths.forEach((depth, index) => view.setUint16(index * 2, depth, true))
  let binary = ''
  const chunk = 0x8000
  for (let start = 0; start < bytes.length; start += chunk) {
    binary += String.fromCharCode(...bytes.subarray(start, start + chunk))
  }
  return btoa(binary)
}
