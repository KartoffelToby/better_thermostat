// The site navigation. Starlight renders it as the sidebar and llms.txt lists
// the same groups in the same order, so a page added here shows up in both.
export const sidebar = [
  {
    label: "Start Here",
    items: [
      { label: "5-minute quickstart", link: "/setup/getting-started/" },
      { label: "How it works & use cases", link: "/setup/use-cases/" },
      {
        label: "Setup dialog walkthrough",
        link: "/setup/configuration-walkthrough/",
      },
      { label: "Interactive Configurator", link: "/setup/configurator/" },
      {
        label: "Recommended settings",
        link: "/optimal-settings/recommended-settings/",
      },
    ],
  },
  {
    label: "Algorithms & Settings",
    items: [
      {
        label: "Calibration types & algorithms",
        link: "/calibration_algorithms/",
      },
      {
        label: "Quick algorithm selector",
        link: "/optimal-settings/algorithm-selection/",
      },
      { label: "All settings reference", link: "/configuration/" },
      { label: "Presets", link: "/setup/presets/" },
      { label: "Entities & attributes", link: "/setup/entities/" },
    ],
  },
  {
    label: "Schedules, Boiler & Automations",
    items: [
      {
        label: "Blueprints & schedules",
        link: "/setup/automation-blueprints/",
      },
      {
        label: "Central heating & boiler control",
        link: "/deep-explanations/central-heating-thermostat/",
      },
      { label: "Pausing learning", link: "/setup/pause-learning/" },
      {
        label: "Releasing devices (manual control)",
        link: "/setup/releasing-devices/",
      },
      { label: "Automation recipes", link: "/setup/automation-recipes/" },
      {
        label: "Actions & conditions",
        link: "/setup/actions-and-conditions/",
      },
    ],
  },
  {
    label: "Hardware & Compatibility",
    items: [
      {
        label: "Supported devices & compatibility",
        link: "/working-devices/compatibility/",
      },
      {
        label: "Recommended TRVs & Zigbee",
        link: "/working-devices/recommended-devices/",
      },
    ],
  },
  {
    label: "Troubleshooting & FAQ",
    items: [
      { label: "Troubleshooting & FAQ", link: "/faq/common-questions/" },
      {
        label: "Summer mode explained",
        link: "/deep-explanations/summer-mode/",
      },
      {
        label: "Degraded mode & sensor fallback",
        link: "/faq/degraded-mode/",
      },
      { label: "Window sensor", link: "/faq/window-sensor/" },
      { label: "Door sensor", link: "/faq/door-sensor/" },
      {
        label: "Missing entity & availability guard",
        link: "/faq/missing-entity/",
      },
      {
        label: "Target temperature not taken",
        link: "/faq/setpoint-not-applied/",
      },
      {
        label: "Thermostat in multiple rooms",
        link: "/faq/shared-trv/",
      },
      {
        label: "Invalid external temperature",
        link: "/faq/invalid-external-temperature/",
      },
      {
        label: "Calibration offset at its limit",
        link: "/faq/calibration-offset-at-limit/",
      },
      { label: "Debugging & diagnostics", link: "/faq/debugging/" },
    ],
  },
  {
    label: "Upgrading & Deep Dives",
    items: [
      {
        label: "Upgrading from 1.9 to 2.0",
        link: "/setup/upgrading-to-2-0/",
      },
      {
        label: "Migrating legacy schedule actions",
        link: "/deep-explanations/schedule-and-night-mode/",
      },
      {
        label: "Virtual hydraulic balancing",
        link: "/deep-explanations/hydraulic-balance/",
      },
      {
        label: "Data updates & radio limits",
        link: "/deep-explanations/data-updates/",
      },
      {
        label: "Removing Better Thermostat",
        link: "/setup/removing/",
      },
    ],
  },
  {
    label: "Internals (Developers)",
    collapsed: true,
    items: [{ autogenerate: { directory: "internals" } }],
  },
  {
    label: "Support",
    link: "/support/",
  },
];
