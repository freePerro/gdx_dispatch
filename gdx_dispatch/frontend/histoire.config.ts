import { defineConfig } from 'histoire'
import { HstVue } from '@histoire/plugin-vue'

export default defineConfig({
  plugins: [HstVue()],
  setupFile: './src/histoire-setup.ts',
  storyMatch: ['src/**/*.story.vue'],
  tree: {
    groups: [
      { title: 'Views', include: file => file.path.includes('views/') },
      { title: 'Components', include: file => file.path.includes('components/') },
    ],
  },
})
