<script setup lang="ts">
import {
  BlueBanner,
  BlueButton,
  BlueButtonGroup,
  BlueIcon,
  BlueInput,
  BlueMenu,
  type BlueMenuItem,
  BluePromptDialog,
  BlueStat,
  BlueSwitch,
  useBlueLoading,
  useBlueSnackbar,
} from '@bluerobotics/bluevue'
import { computed, onBeforeUnmount, ref, shallowRef } from 'vue'

import SeafloorMap from '@/components/SeafloorMap.vue'
import { isSitl } from '@/composables/vehicleStatus'
import { buildGrid, encodeGrid, gridBounds, METERS_PER_DEGREE } from '@/seafloor/grid'
import { cssColor, renderGrid, type SeafloorOverlay } from '@/seafloor/render'
import { parseSurvey, selectSoundings, type SurveyRow } from '@/seafloor/survey'
import { MAX_PRESETS, SeafloorApi } from '@/services/api'
import type { Seafloor, SitlLocation, SonarNoise, SonarSettings, SonarState } from '@/types/sitl'

const emit = defineEmits<{ (event: 'spawn', location: SitlLocation): void }>()

const { notify, notifyError } = useBlueSnackbar()
const { showLoading, hideLoading } = useBlueLoading()

const library = ref<Seafloor[]>([])
// The seafloor on the map, which is also the one the sonar is set to sound.
const viewing = ref('')
const viewedOverlay = shallowRef<SeafloorOverlay | null>(null)
// Rendered grids by name, so moving between seafloors does not fetch and draw them again.
const overlays = new Map<string, SeafloorOverlay>()

const settings = ref<SonarSettings>({ enabled: false, seafloor: null, noise: 'realistic' })
const sonar = ref<SonarState | null>(null)
const busy = ref(false)

const READING_INTERVAL_MS = 1000

// ---- Loading a survey ------------------------------------------------------------------------

// A survey being turned into a seafloor: parsed once, then rebuilt as the options change.
interface Draft {
  file: string
  rows: SurveyRow[]
}

const draft = shallowRef<Draft | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
// Simple Ping Survey only maps soundings it is at least this sure of, so neither does this.
const minConfidence = ref(90)
// Empty picks the finest cell that keeps the grid within the backend's limit.
const cellSizeInput = ref<number | string>('')
// Wide enough to join the lines of a typical single-beam survey, which run 10 to 40 m apart.
const maxGap = ref(50)
const saveDialogOpen = ref(false)

const draftResult = computed(() => {
  const current = draft.value
  if (!current) {
    return null
  }
  try {
    const soundings = selectSoundings(current.rows, Number(minConfidence.value))
    const requested = Number(cellSizeInput.value)
    const grid = buildGrid(soundings.rows, {
      cellSize: cellSizeInput.value === '' || !(requested > 0) ? null : requested,
      maxGap: Math.max(Number(maxGap.value), 0),
    })
    const overlay: SeafloorOverlay = {
      url: renderGrid(grid.depths, grid.rows, grid.cols, grid.depthMin, grid.depthMax),
      bounds: gridBounds(grid),
    }
    return { soundings, grid, overlay, error: '' }
  } catch (error) {
    return { soundings: null, grid: null, overlay: null, error: (error as Error).message }
  }
})

const draftSummary = computed(() => {
  const result = draftResult.value
  if (!result?.grid || !result.soundings) {
    return ''
  }
  const { grid, soundings } = result
  const width = grid.cols * grid.cellSize
  const height = grid.rows * grid.cellSize
  const covered = (100 * grid.filled) / (grid.rows * grid.cols)
  return (
    `${soundings.rows.length.toLocaleString()} soundings kept, ${soundings.dropped.toLocaleString()} left out. ` +
    `${width.toFixed(0)} × ${height.toFixed(0)} m in ${grid.cellSize} m cells, ${covered.toFixed(0)}% covered, ` +
    `${grid.depthMin.toFixed(1)}–${grid.depthMax.toFixed(1)} m deep.`
  )
})

function chooseFile(): void {
  fileInput.value?.click()
}

async function onFileSelected(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) {
    return
  }
  try {
    const rows = parseSurvey(await file.text())
    draft.value = { file: file.name, rows }
  } catch (error) {
    notifyError(error, `Could not read ${file.name}`)
  }
}

function cancelDraft(): void {
  draft.value = null
}

const draftName = computed(() => draft.value?.file.replace(/\.csv$/i, '') ?? '')

async function saveDraft(name: string): Promise<void> {
  const result = draftResult.value
  const current = draft.value
  if (!result?.grid || !result.soundings || !result.overlay || !current) {
    return
  }
  const { grid, soundings, overlay } = result
  busy.value = true
  try {
    const saved = await SeafloorApi.save({
      name,
      source: current.file,
      origin_latitude: grid.originLatitude,
      origin_longitude: grid.originLongitude,
      cell_size: grid.cellSize,
      rows: grid.rows,
      cols: grid.cols,
      depth_min: grid.depthMin,
      depth_max: grid.depthMax,
      soundings: soundings.rows.length,
      spawn: soundings.start,
      grid: encodeGrid(grid.depths),
    })
    overlays.set(saved.name, overlay)
    draft.value = null
    await loadLibrary()
    await view(saved.name)
    notify(`Saved seafloor "${saved.name}".`, { severity: 'success' })
  } catch (error) {
    notifyError(error, 'Could not save the seafloor')
  } finally {
    busy.value = false
  }
}

// ---- The library -----------------------------------------------------------------------------

const viewed = computed(() => library.value.find((entry) => entry.name === viewing.value) ?? null)

async function view(name: string): Promise<void> {
  viewing.value = name
  settings.value = { ...settings.value, seafloor: name || null }
  if (!name) {
    viewedOverlay.value = null
    return
  }
  const cached = overlays.get(name)
  if (cached) {
    viewedOverlay.value = cached
    return
  }
  const entry = library.value.find((candidate) => candidate.name === name)
  if (!entry) {
    return
  }
  try {
    const depths = await SeafloorApi.grid(name)
    const overlay: SeafloorOverlay = {
      url: renderGrid(depths, entry.rows, entry.cols, entry.depth_min, entry.depth_max),
      bounds: gridBounds({
        originLatitude: entry.origin_latitude,
        originLongitude: entry.origin_longitude,
        cellSize: entry.cell_size,
        rows: entry.rows,
        cols: entry.cols,
      }),
    }
    overlays.set(name, overlay)
    if (viewing.value === name) {
      viewedOverlay.value = overlay
    }
  } catch (error) {
    notifyError(error, `Could not load seafloor "${name}"`)
  }
}

const libraryButtons = computed(() =>
  library.value.map((entry) => ({
    name: entry.name,
    tooltip: entry.source ? `From ${entry.source}` : undefined,
    preSelected: entry.name === viewing.value,
    onSelected: () => view(entry.name),
  })),
)

const libraryFull = computed(() => library.value.length >= MAX_PRESETS)

const libraryActions = computed<BlueMenuItem[]>(() => [
  {
    title: 'Load a survey CSV…',
    icon: 'mdi-file-upload-outline',
    disabled: libraryFull.value,
    hint: libraryFull.value ? `The row holds ${MAX_PRESETS} seafloors; delete one first` : undefined,
    action: chooseFile,
  },
])
const libraryMenuOpen = ref(false)

const contextEntry = ref<Seafloor | null>(null)
const contextTarget = ref<[number, number]>([0, 0])
const contextMenuOpen = ref(false)
const renameDialogOpen = ref(false)

const contextItems = computed<BlueMenuItem[]>(() => {
  const entry = contextEntry.value
  if (!entry) {
    return []
  }
  return [
    { title: 'Use as spawn point', icon: 'mdi-map-marker-outline', action: () => useAsSpawn(entry) },
    { title: 'Rename…', icon: 'mdi-rename-box-outline', action: () => (renameDialogOpen.value = true) },
    { title: 'Delete', icon: 'mdi-delete-outline', danger: true, action: () => removeEntry(entry) },
  ]
})

function onLibraryContextMenu({ item, x, y }: { item: { name: string }; x: number; y: number }): void {
  const entry = library.value.find((candidate) => candidate.name === item.name)
  if (!entry) {
    return
  }
  contextEntry.value = entry
  contextTarget.value = [x, y]
  contextMenuOpen.value = true
}

async function confirmRename(newName: string): Promise<void> {
  const entry = contextEntry.value
  if (!entry) {
    return
  }
  try {
    const renamed = await SeafloorApi.rename(entry.name, newName)
    const overlay = overlays.get(entry.name)
    if (overlay) {
      overlays.set(renamed.name, overlay)
      overlays.delete(entry.name)
    }
    await loadLibrary()
    await refreshSonar()
    if (viewing.value === entry.name) {
      await view(renamed.name)
    }
  } catch (error) {
    notifyError(error, 'Could not rename the seafloor')
  }
}

async function removeEntry(entry: Seafloor): Promise<void> {
  try {
    notify((await SeafloorApi.remove(entry.name)).detail, { severity: 'success' })
    overlays.delete(entry.name)
    await loadLibrary()
    await refreshSonar()
    if (viewing.value === entry.name) {
      await view(library.value[0]?.name ?? '')
    }
  } catch (error) {
    notifyError(error, 'Could not delete the seafloor')
  }
}

// Where the survey started, or the middle of the grid for one that does not say.
function useAsSpawn(entry: Seafloor): void {
  const centre = (): SitlLocation => {
    const metersPerDegreeLon = METERS_PER_DEGREE * Math.cos((entry.origin_latitude * Math.PI) / 180)
    return {
      latitude: entry.origin_latitude + ((entry.rows - 1) * entry.cell_size) / 2 / METERS_PER_DEGREE,
      longitude: entry.origin_longitude + ((entry.cols - 1) * entry.cell_size) / 2 / metersPerDegreeLon,
      altitude: 0,
      heading: 0,
    }
  }
  const spawn = entry.spawn ?? centre()
  emit('spawn', {
    latitude: Number(spawn.latitude.toFixed(6)),
    longitude: Number(spawn.longitude.toFixed(6)),
    altitude: 0,
    heading: Math.round(spawn.heading),
  })
}

const viewedSummary = computed(() => {
  const entry = viewed.value
  if (!entry) {
    return ''
  }
  const width = entry.cols * entry.cell_size
  const height = entry.rows * entry.cell_size
  return (
    `${width.toFixed(0)} × ${height.toFixed(0)} m in ${entry.cell_size} m cells, ` +
    `${entry.depth_min.toFixed(1)}–${entry.depth_max.toFixed(1)} m deep, ` +
    `from ${entry.soundings.toLocaleString()} soundings${entry.source ? ` in ${entry.source}` : ''}.`
  )
})

// ---- The sonar -------------------------------------------------------------------------------

const noiseButtons = computed(() =>
  (
    [
      ['off', 'Off', 'The seafloor exactly as surveyed'],
      ['realistic', 'Realistic', "Scatter of 0.5% of the range, the Ping2's resolution"],
      ['high', 'High', 'Scatter of 2% of the range, a sonar working in poor conditions'],
    ] as [SonarNoise, string, string][]
  ).map(([noise, name, tooltip]) => ({
    name,
    tooltip,
    preSelected: settings.value.noise === noise,
    onSelected: () => (settings.value = { ...settings.value, noise }),
  })),
)

// A sonar the autopilot is not set up to read also has something to apply: the setup.
const pendingChange = computed(() => {
  const running = sonar.value?.settings
  return (
    running === undefined ||
    (settings.value.enabled && sonar.value?.rangefinder_ready === false) ||
    running.enabled !== settings.value.enabled ||
    running.noise !== settings.value.noise ||
    (settings.value.enabled && running.seafloor !== settings.value.seafloor)
  )
})

const stats = computed(() => {
  const state = sonar.value
  const reading = state?.reading
  if (!state || !reading) {
    return []
  }
  return [
    { label: 'Range', value: reading.bottom ? `${reading.distance.toFixed(2)} m` : 'No bottom' },
    { label: 'Confidence', value: `${reading.confidence} %` },
    { label: 'Autopilot', value: state.autopilot_receiving ? 'Reading it' : 'Not reading it' },
  ]
})

const banner = computed(() => {
  const state = sonar.value
  if (!state?.settings.enabled) {
    return ''
  }
  if (state.idle_reason) {
    return state.idle_reason
  }
  if (state.rangefinder_ready === false) {
    return (
      "The autopilot's first rangefinder is not set to MAVLink, so it is not taking the soundings. " +
      'Apply sets it up, which restarts the autopilot.'
    )
  }
  if (state.reading && !state.reading.bottom) {
    return 'The vehicle is off the survey, so the sonar finds no bottom and reports the full range at 1% confidence.'
  }
  return ''
})

const vehicle = computed(() => {
  const reading = sonar.value?.reading
  if (!reading || (reading.latitude === 0 && reading.longitude === 0)) {
    return null
  }
  return { latitude: reading.latitude, longitude: reading.longitude, bottom: reading.bottom }
})

let poll: ReturnType<typeof setInterval> | null = null

function stopPolling(): void {
  if (poll !== null) {
    clearInterval(poll)
    poll = null
  }
}

async function refreshSonar(): Promise<void> {
  try {
    sonar.value = await SeafloorApi.sonar()
    if (sonar.value.settings.enabled) {
      if (poll === null) {
        poll = setInterval(refreshSonar, READING_INTERVAL_MS)
      }
    } else {
      stopPolling()
    }
  } catch {
    // Whatever went wrong will still be wrong in a second, and a notice every second is worse
    // than the last reading staying on screen.
    stopPolling()
  }
}

onBeforeUnmount(stopPolling)

async function loadLibrary(): Promise<void> {
  try {
    library.value = await SeafloorApi.library()
  } catch (error) {
    notifyError(error, 'Could not load the seafloors')
  }
}

async function reload(): Promise<void> {
  await Promise.all([loadLibrary(), refreshSonar()])
  const running = sonar.value?.settings
  if (running) {
    settings.value = { ...running }
  }
  const stillThere = library.value.some((entry) => entry.name === viewing.value)
  await view(running?.seafloor ?? (stillThere ? viewing.value : library.value[0]?.name ?? ''))
}

defineExpose({ reload })

async function apply(): Promise<void> {
  // Turning the rangefinder on is the one change ArduPilot only takes at boot.
  const restarts = settings.value.enabled && sonar.value?.rangefinder_ready !== true
  busy.value = true
  if (restarts) {
    showLoading('Starting the sonar… the autopilot may restart to take it as its rangefinder, which can take up to a minute.')
  }
  try {
    notify((await SeafloorApi.setSonar(settings.value)).detail, { severity: 'success' })
  } catch (error) {
    notifyError(error, 'Could not set the simulated sonar')
  } finally {
    busy.value = false
    if (restarts) {
      hideLoading()
    }
  }
  await refreshSonar()
}

const legend = computed(() => {
  const entry = draftResult.value?.grid
  const low = entry ? entry.depthMin : viewed.value?.depth_min
  const high = entry ? entry.depthMax : viewed.value?.depth_max
  if (low === undefined || high === undefined) {
    return null
  }
  return {
    low: `${low.toFixed(1)} m`,
    high: `${high.toFixed(1)} m`,
    gradient: `linear-gradient(to right, ${[0, 0.2, 0.4, 0.6, 0.8, 1].map(cssColor).join(', ')})`,
  }
})
</script>

<template>
  <div class="flex flex-col gap-4">
    <BlueBanner
      v-if="!isSitl"
      severity="warning"
      text="The active board is not SITL; the simulated sonar only exists in the simulator."
    />

    <div class="flex items-center gap-2">
      <div class="flex-1 min-w-0">
        <BlueButtonGroup
          v-if="libraryButtons.length"
          :key="`${library.length}-${viewing}`"
          label="Seafloors"
          theme="dark"
          type="switch"
          :disabled="busy || draft !== null"
          :button-items="libraryButtons"
          info-tooltip="Surveys turned into seafloors. Selecting one shows it on the map and makes it the one the sonar sounds once applied. Hold or right-click one for its own actions."
          @context-menu="onLibraryContextMenu"
        />
        <div
          v-else-if="draft === null"
          class="text-sm text-[#ffffff88]"
        >
          No seafloors yet. Load a Simple Ping Survey log to make one.
        </div>
      </div>
      <BlueButton
        v-if="!libraryButtons.length && draft === null"
        density="compact"
        theme="dark"
        icon="mdi-file-upload-outline"
        @click="chooseFile"
      >
        Load survey CSV
      </BlueButton>
      <BlueMenu
        v-model="libraryMenuOpen"
        :items="libraryActions"
      >
        <template #activator="{ props: menuProps }">
          <button
            v-bind="menuProps"
            class="shrink-0 rounded-[6px] px-1 py-1 text-[#ffffffaa] hover:text-white transition-colors"
            :class="busy || draft !== null ? 'opacity-50 pointer-events-none' : 'cursor-pointer'"
            title="Seafloor actions"
          >
            <BlueIcon name="mdi-dots-vertical" />
          </button>
        </template>
      </BlueMenu>
      <BlueMenu
        v-model="contextMenuOpen"
        :items="contextItems"
        :target="contextTarget"
      />
      <input
        ref="fileInput"
        type="file"
        accept="text/csv,.csv"
        class="hidden"
        @change="onFileSelected"
      >
    </div>

    <div
      v-if="draft !== null"
      class="flex flex-col gap-3"
    >
      <div class="text-base font-bold uppercase tracking-wide text-[#989898] truncate">
        New seafloor from {{ draft.file }}
      </div>
      <div class="flex flex-wrap gap-3">
        <BlueInput
          v-model="minConfidence"
          name="min-confidence"
          label="Minimum confidence"
          type="number"
          theme="dark"
          width="240px"
          suffix="%"
          :min="0"
          :max="100"
          :step="1"
          info-tooltip="Soundings the Ping was less sure of are left out. Simple Ping Survey maps 90% and above."
        />
        <BlueInput
          v-model="cellSizeInput"
          name="cell-size"
          label="Cell size"
          type="number"
          theme="dark"
          width="240px"
          suffix="m"
          :min="0.5"
          :step="0.5"
          :placeholder="draftResult?.grid ? `Auto (${draftResult.grid.cellSize})` : 'Auto'"
          info-tooltip="Metres between depths in the grid. Left empty, the finest that keeps the grid under a million cells."
        />
        <BlueInput
          v-model="maxGap"
          name="max-gap"
          label="Fill gaps up to"
          type="number"
          theme="dark"
          width="240px"
          suffix="m"
          :min="0"
          :step="1"
          info-tooltip="Survey lines closer than this are joined up, filling the water between them. Further apart, the gap is left empty, and the sonar finds no bottom there."
        />
      </div>
      <BlueBanner
        v-if="draftResult?.error"
        severity="error"
        :text="draftResult.error"
      />
      <div
        v-else
        class="text-sm text-[#ffffff88]"
      >
        {{ draftSummary }}
      </div>
      <div class="flex justify-end gap-2">
        <BlueButton
          density="compact"
          theme="dark"
          @click="cancelDraft"
        >
          Cancel
        </BlueButton>
        <BlueButton
          variant="filled"
          theme="dark"
          :loading="busy"
          :disabled="!draftResult?.grid"
          @click="saveDialogOpen = true"
        >
          Save seafloor
        </BlueButton>
      </div>
    </div>

    <SeafloorMap
      :overlay="draft !== null ? draftResult?.overlay ?? null : viewedOverlay"
      :vehicle="draft !== null ? null : vehicle"
      :start="draft !== null ? draftResult?.soundings?.start ?? null : viewed?.spawn ?? null"
    />

    <div
      v-if="legend"
      class="flex items-center gap-2 text-sm text-[#ffffff88]"
    >
      <span>{{ legend.low }}</span>
      <div
        class="h-2 w-40 rounded"
        :style="{ background: legend.gradient }"
      />
      <span>{{ legend.high }}</span>
    </div>

    <div
      v-if="draft === null && viewed"
      class="flex items-center justify-between gap-2"
    >
      <div class="text-sm text-[#ffffff88]">
        {{ viewedSummary }}
      </div>
      <BlueButton
        density="compact"
        theme="dark"
        icon="mdi-map-marker-outline"
        @click="useAsSpawn(viewed)"
      >
        Use as spawn point
      </BlueButton>
    </div>

    <div :class="{ 'opacity-50 pointer-events-none': !isSitl || draft !== null }">
      <div class="mx-auto mb-4 h-px w-[70%] bg-[#ffffff0b]" />
      <div class="text-base font-bold uppercase tracking-wide text-[#989898] mt-[5px] mb-[17px] truncate">
        Sonar
      </div>
      <div class="flex flex-col gap-4">
        <BlueBanner
          v-if="banner"
          severity="warning"
          :text="banner"
        />
        <BlueSwitch
          v-model="settings.enabled"
          name="simulated-sonar"
          label="Sound the seafloor"
          theme="dark"
          :disabled="busy || (!settings.seafloor && !settings.enabled)"
          info-tooltip="A Ping2 under the boat, sounding the selected seafloor wherever the vehicle drives. Its readings reach Simple Ping Survey and the autopilot just as a real Ping's do, and the autopilot reports them on to Cockpit."
        />
        <BlueButtonGroup
          :key="settings.noise"
          label="Noise"
          theme="dark"
          type="switch"
          :button-items="noiseButtons"
          info-tooltip="How much the readings scatter around the surveyed depth."
        />
        <div
          v-if="stats.length"
          class="flex flex-wrap items-stretch gap-2"
        >
          <BlueStat
            v-for="stat in stats"
            :key="stat.label"
            :label="stat.label"
            :value="stat.value"
          />
        </div>
        <div class="flex items-center justify-end gap-2">
          <BlueButton
            variant="filled"
            theme="dark"
            :loading="busy"
            :disabled="!isSitl || !pendingChange || (settings.enabled && !settings.seafloor)"
            @click="apply"
          >
            Apply
          </BlueButton>
        </div>
      </div>
    </div>

    <BluePromptDialog
      v-model="saveDialogOpen"
      icon="mdi-waves"
      title="Save seafloor"
      subtitle="Stores the grid on the vehicle, so the sonar can sound it."
      label="Seafloor name"
      :initial="draftName"
      @confirm="saveDraft"
    />

    <BluePromptDialog
      v-model="renameDialogOpen"
      icon="mdi-rename-box-outline"
      title="Rename seafloor"
      label="New name"
      confirm-label="Rename"
      :initial="contextEntry?.name"
      @confirm="confirmRename"
    />
  </div>
</template>
