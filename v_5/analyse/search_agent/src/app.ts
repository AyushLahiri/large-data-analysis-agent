import express from 'express';
import { createHash, timingSafeEqual } from 'node:crypto';
import { researchRoutes } from './orchestration/routes';
import { scopeSchema } from './orchestration/session';

const app=express();
app.get('/health',(_req,res)=>{res.json({status:'ok'});});
const secret=process.env.V5_ANALYSIS_SEARCH_TOKEN;
if(!secret || secret.length<32) throw Error('Configure the internal analysis-search service token');
const hash=(value:string)=>createHash('sha256').update(value).digest();
const expected=hash('Bearer '+secret);
app.use((req,res,next)=>{
  if(!timingSafeEqual(hash(req.get('Authorization') || ''),expected)){
    res.sendStatus(401);return;
  }
  const scope=scopeSchema.safeParse({project_id:req.get('X-Project-Id'),
    chat_id:req.get('X-Chat-Id'),session_id:req.get('X-Session-Id')});
  if(!scope.success){res.status(400).json({error:'Supply project, chat and live session IDs'});return;}
  res.locals.scope=scope.data;
  next();
});
app.use(express.json({limit:'10mb'}));
app.use(researchRoutes);
export default app;

