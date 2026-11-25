<script setup lang="ts">
import 'leaflet/dist/leaflet.css'

import L from 'leaflet'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { SeafloorOverlay, SonarPosition } from '@/seafloor/render'

const props = defineProps<{
  overlay: SeafloorOverlay | null
  vehicle: SonarPosition | null
  start: { latitude: number; longitude: number } | null
}>()

const mapElement = ref<HTMLElement | null>(null)
let map: L.Map | null = null
let image: L.ImageOverlay | null = null
let vehicleMarker: L.CircleMarker | null = null
let startMarker: L.CircleMarker | null = null

// Found bottom in the theme's primary colour; no bottom in a warning amber, which is what the
// vehicle reads off the edge of the survey.
const BOTTOM_COLOR = 'var(--bluevue-primary)'
const NO_BOTTOM_COLOR = '#f5a623'

function showOverlay(overlay: SeafloorOverlay | null): void {
  image?.remove()
  image = null
  if (!map || !overlay) {
    return
  }
  image = L.imageOverlay(overlay.url, overlay.bounds, { className: 'seafloor-image' }).addTo(map)
  map.fitBounds(overlay.bounds, { padding: [16, 16] })
}

function showVehicle(vehicle: SonarPosition | null): void {
  if (!map) {
    return
  }
  if (!vehicle) {
    vehicleMarker?.remove()
    vehicleMarker = null
    return
  }
  const color = vehicle.bottom ? BOTTOM_COLOR : NO_BOTTOM_COLOR
  if (!vehicleMarker) {
    vehicleMarker = L.circleMarker([vehicle.latitude, vehicle.longitude], {
      radius: 7,
      weight: 2,
      color: '#ffffff',
      fillColor: color,
      fillOpacity: 1,
    }).addTo(map)
    return
  }
  vehicleMarker.setLatLng([vehicle.latitude, vehicle.longitude])
  vehicleMarker.setStyle({ fillColor: color })
}

function showStart(start: { latitude: number; longitude: number } | null): void {
  startMarker?.remove()
  startMarker = null
  if (!map || !start) {
    return
  }
  startMarker = L.circleMarker([start.latitude, start.longitude], {
    radius: 5,
    weight: 2,
    color: '#ffffff',
    fillOpacity: 0,
  })
    .bindTooltip('Where the survey started')
    .addTo(map)
}

onMounted(() => {
  if (!mapElement.value) {
    return
  }
  map = L.map(mapElement.value, { zoomControl: true }).setView([0, 0], 2)
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 21,
    maxNativeZoom: 19,
    attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map)

  showOverlay(props.overlay)
  showStart(props.start)
  showVehicle(props.vehicle)

  // Leaflet measures the container lazily; force a recompute once it is visible.
  setTimeout(() => {
    map?.invalidateSize()
    if (props.overlay) {
      map?.fitBounds(props.overlay.bounds, { padding: [16, 16] })
    }
  }, 0)
})

watch(() => props.overlay, showOverlay)
watch(() => props.start, showStart)
watch(() => props.vehicle, showVehicle)

onBeforeUnmount(() => {
  map?.remove()
  map = null
  image = null
  vehicleMarker = null
  startMarker = null
})
</script>

<template>
  <div
    ref="mapElement"
    class="seafloor-map elevation-1"
  />
</template>

<style scoped>
.seafloor-map {
  height: 320px;
  width: 100%;
  border-radius: 4px;
  z-index: 0;
}

/* One pixel is one cell; smoothing them would draw depths between cells the grid never had. */
:deep(.seafloor-image) {
  image-rendering: pixelated;
}
</style>
