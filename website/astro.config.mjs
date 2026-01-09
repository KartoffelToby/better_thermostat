// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightThemeFlexoki from "starlight-theme-flexoki";

// https://astro.build/config
export default defineConfig({
  integrations: [
    starlight({
      title: "Better Thermostat",
      social: {
        github: "https://github.com/KartoffelToby/better_thermostat",
      },
      sidebar: [
        {
          label: "Configuration",
          autogenerate: { directory: "Configuration" },
        },
        {
          label: "Technical Details",
          autogenerate: { directory: "Technical Details" },
        },
        {
          label: "Q&A",
          autogenerate: { directory: "Q&A" },
        },
        {
          label: "Nice to know",
          items: ["schedule"],
        },
      ],
      plugins: [starlightThemeFlexoki()],
    }),
  ],
});
