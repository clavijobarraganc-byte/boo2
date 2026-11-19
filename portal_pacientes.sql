-- =====================================================================
--  Portal de pacientes · Clínica Camilo's
--  Script para phpMyAdmin (MariaDB / MySQL)
--
--  Crear:  pestaña "Importar" -> seleccionar este archivo -> Continuar
--  O bien: pestaña "SQL" -> pegar el contenido -> Continuar
--
--  Es idempotente: se puede ejecutar más de una vez sin duplicar datos.
-- =====================================================================

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 1. Cuentas de acceso al portal (se enlazan con hospedaje.huesped)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `hospedaje`.`cuentas_paciente` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  `huesped_id` BIGINT UNSIGNED NOT NULL,
  `correo` VARCHAR(254) NOT NULL,
  `password_hash` VARCHAR(255) NOT NULL,
  `activo` TINYINT(1) NOT NULL DEFAULT 1,
  `creado_en` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY `uq_cuenta_correo` (`correo`),
  UNIQUE KEY `uq_cuenta_huesped` (`huesped_id`),
  CONSTRAINT `fk_cuenta_huesped` FOREIGN KEY (`huesped_id`)
    REFERENCES `hospedaje`.`huesped` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 2. Vínculo de cada cita con el paciente que la solicitó
--    (si tu MySQL no soporta IF NOT EXISTS, ejecuta solo el ALTER)
-- ---------------------------------------------------------------------
ALTER TABLE `hospedaje`.`citas`
  ADD COLUMN IF NOT EXISTS `huesped_id` BIGINT UNSIGNED NULL AFTER `id`;

ALTER TABLE `hospedaje`.`citas`
  ADD INDEX IF NOT EXISTS `idx_citas_huesped` (`huesped_id`);

-- ---------------------------------------------------------------------
-- 3. Enlazar citas antiguas creadas por nombre, solo cuando el nombre
--    completo identifica a un único paciente (evita cruces de datos).
-- ---------------------------------------------------------------------
UPDATE `hospedaje`.`citas` c
  JOIN `hospedaje`.`huesped` h
    ON c.`paciente` = CONCAT(h.`nombre`, ' ', h.`apellido`)
   SET c.`huesped_id` = h.`id`
 WHERE c.`huesped_id` IS NULL
   AND (SELECT COUNT(*) FROM `hospedaje`.`huesped` x
         WHERE CONCAT(x.`nombre`, ' ', x.`apellido`) = c.`paciente`) = 1;

-- Comprobación rápida:
-- SELECT c.id, c.paciente, c.huesped_id, c.fecha, c.hora, c.estado
--   FROM hospedaje.citas c ORDER BY c.id DESC LIMIT 10;
