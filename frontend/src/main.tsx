import { QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { createQueryClient } from './api/query-client'
import { SessionGate } from './features/session/session-gate'
import { migrateStorageKeys } from './lib/storage-keys'
import { createAppRouter } from './router'

import './index.css'

migrateStorageKeys() // before anything reads a saved choice (#661)

const queryClient = createQueryClient()
const router = createAppRouter()

const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('index.html has no #root element')
}

createRoot(rootElement).render(
  <StrictMode>
    <SessionGate>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </SessionGate>
  </StrictMode>,
)
