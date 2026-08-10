<objetivo>
{goal}
</objetivo>

<historial_conversacion>
{prior_context}
</historial_conversacion>

<herramientas_disponibles>
{tools}
</herramientas_disponibles>

<tarea_a_evaluar>
{task}
</tarea_a_evaluar>

Evalúa únicamente la tarea dentro de <tarea_a_evaluar> (puede ser el objetivo completo o una subtarea de un
nivel anterior), apoyándote en <historial_conversacion> solo como contexto de fondo si es relevante y en
<herramientas_disponibles> según el criterio de <herramientas_y_atomicidad> de tus instrucciones, y responde
con el JSON de clasificación indicado en tus instrucciones.
