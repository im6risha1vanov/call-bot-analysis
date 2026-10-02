from queue_runner import on_startup, register
from automatic import jobs


@on_startup
async def recover_automatic_combined(pool):
    await pool.execute("UPDATE automatic_call_reports SET status='uncertain',error='restart during request' WHERE status='processing' AND response_text IS NULL")


@register('automatic_combined_call')
async def automatic_combined_call(pool,task):
    return await jobs.run(pool,task)
