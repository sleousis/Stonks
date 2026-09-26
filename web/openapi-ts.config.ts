import { defineConfig } from '@hey-api/openapi-ts';

// Generates the typed REST client from the checked-in contract (openapi.json).
// Regenerate with `npm run api:generate` after the backend refreshes the
// contract (`uv run python -m stonks.api.openapi`). Never edit the output.
export default defineConfig({
  input: './openapi.json',
  output: {
    path: './src/app/api/generated',
    postProcess: ['prettier'],
  },
  plugins: ['@hey-api/client-angular', '@hey-api/typescript', '@hey-api/sdk'],
});
