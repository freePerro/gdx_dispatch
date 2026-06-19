import { defineSetupVue3 } from '@histoire/plugin-vue'
import PrimeVue from 'primevue/config'
import ToastService from 'primevue/toastservice'
import ConfirmationService from 'primevue/confirmationservice'
import Aura from '@primevue/themes/aura'

export const setupVue3 = defineSetupVue3(({ app }) => {
  app.use(PrimeVue, { theme: { preset: Aura } })
  app.use(ToastService)
  app.use(ConfirmationService)
})
