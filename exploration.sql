-- Средняя температура по локациям
SELECT [Location], AVG(SST_C) AS avg_sst
FROM dbo.realistic_ocean_climate_dataset
GROUP BY [Location]
ORDER BY avg_sst DESC;

-- Сколько случаев по уровню bleaching
SELECT Bleaching_Severity, COUNT(*) AS cnt
FROM dbo.realistic_ocean_climate_dataset
GROUP BY Bleaching_Severity
ORDER BY cnt DESC;

-- Доля heatwave = 1
SELECT AVG(CAST(Marine_Heatwave AS float)) AS heatwave_share
FROM dbo.realistic_ocean_climate_dataset;
