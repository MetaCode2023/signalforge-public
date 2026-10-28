import {readFile} from 'node:fs/promises';
import {submitPaperReport} from '../src/client.js';

const [endpoint,filename]=process.argv.slice(2);
if(!endpoint || !filename){
  process.stderr.write('Usage: node scripts/send-report.mjs https://PRIVATE_HOST/v1/ingest report.json\n');
  process.exitCode=2;
}else{
  try{
    const payload=JSON.parse(await readFile(filename,'utf8'));
    const result=await submitPaperReport(endpoint,payload,{
      ingestSecret:process.env.SIGNALFORGE_INGEST_SECRET,
      accessClientId:process.env.SIGNALFORGE_ACCESS_CLIENT_ID,
      accessClientSecret:process.env.SIGNALFORGE_ACCESS_CLIENT_SECRET
    });
    process.stdout.write(JSON.stringify(result)+'\n');
    if(!result.ok)process.exitCode=1;
  }catch(error){process.stderr.write(`Report not sent: ${error.message}\n`);process.exitCode=1;}
}
