import { NO_DATA } from '@/seafloor/grid'

// A rendered grid and where it lies, as the map lays it out.
export interface SeafloorOverlay {
  url: string
  bounds: [[number, number], [number, number]]
}

// Where the sonar last sounded, and whether the beam found the seafloor there.
export interface SonarPosition {
  latitude: number
  longitude: number
  bottom: boolean
}

// Shallow to deep, pale to dark, the way a chart shades its soundings.
const RAMP: [number, number, number][] = [
  [237, 248, 177],
  [127, 205, 187],
  [65, 182, 196],
  [29, 145, 192],
  [34, 94, 168],
  [12, 44, 132],
]

/** The ramp's colour for a fraction of the way from shallowest to deepest. */
export function depthColor(fraction: number): [number, number, number] {
  const position = Math.min(Math.max(fraction, 0), 1) * (RAMP.length - 1)
  const low = Math.floor(position)
  const high = Math.min(low + 1, RAMP.length - 1)
  const reach = position - low
  return RAMP[low].map((channel, index) => Math.round(channel + (RAMP[high][index] - channel) * reach)) as [
    number,
    number,
    number,
  ]
}

export function cssColor(fraction: number): string {
  return `rgb(${depthColor(fraction).join(',')})`
}

/**
 * Draw a depth grid as an image, one pixel a cell, for laying over the map. The grid runs from
 * the south and an image from the top, so rows are drawn upside down. Cells without data are
 * left transparent, which is what shows where the sonar will find no bottom.
 */
export function renderGrid(depths: Uint16Array, rows: number, cols: number, depthMin: number, depthMax: number): string {
  const canvas = document.createElement('canvas')
  canvas.width = cols
  canvas.height = rows
  const context = canvas.getContext('2d')
  if (!context) {
    return ''
  }
  const image = context.createImageData(cols, rows)
  const span = Math.max(depthMax - depthMin, 0.01)
  for (let row = 0; row < rows; row++) {
    const target = (rows - 1 - row) * cols
    for (let col = 0; col < cols; col++) {
      const depth = depths[row * cols + col]
      if (depth === NO_DATA) {
        continue
      }
      const [red, green, blue] = depthColor((depth / 100 - depthMin) / span)
      const pixel = (target + col) * 4
      image.data[pixel] = red
      image.data[pixel + 1] = green
      image.data[pixel + 2] = blue
      image.data[pixel + 3] = 210
    }
  }
  context.putImageData(image, 0, 0)
  return canvas.toDataURL('image/png')
}
