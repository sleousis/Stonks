// Regenerates the API client and fails if the result differs from what git
// has (committed or staged). CI runs it on a clean checkout.
import { execSync } from 'node:child_process';

const DIR = 'src/app/api/generated src/app/core/auth/route-permissions.gen.ts';
const git = (args) => execSync(`git ${args} -- ${DIR}`, { encoding: 'utf8' }).trim();

execSync('npx openapi-ts', { stdio: 'inherit' });
execSync('node scripts/gen-permissions.mjs', { stdio: 'inherit' });
const changed = git('diff --name-only');
const added = git('ls-files --others --exclude-standard');
if (changed || added) {
  console.error(
    `The generated API client is out of date with openapi.json:\n${changed}\n${added}\n` +
      'Run `npm run api:generate` in web/ and commit the result.',
  );
  process.exit(1);
}
console.log('Generated API client matches openapi.json.');
