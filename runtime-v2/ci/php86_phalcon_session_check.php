<?php

if (PHP_VERSION_ID < 80600 || PHP_VERSION_ID >= 80700) {
    return;
}

// Exercise SID generation without constructing storage adapters or opening connections.
(static function (): void {
    $requireSid = static function (mixed $sid): string {
        if (!is_string($sid) || $sid === ''
            || strlen($sid) !== (int) ini_get('session.sid_length')
            || preg_match('/\A[A-Za-z0-9,-]+\z/D', $sid) !== 1) {
            throw new RuntimeException('Phalcon generated an invalid session ID');
        }
        return $sid;
    };

    $method = new ReflectionMethod(Phalcon\Session\Adapter\AbstractAdapter::class, 'create_sid');
    if (!$method->isPublic() || $method->getNumberOfParameters() !== 0
        || (string) $method->getReturnType() !== 'string') {
        throw new RuntimeException('Phalcon AbstractAdapter SID method contract mismatch');
    }

    $ids = [];
    foreach ([
        Phalcon\Session\Adapter\Noop::class,
        Phalcon\Session\Adapter\Stream::class,
        Phalcon\Session\Adapter\Libmemcached::class,
        Phalcon\Session\Adapter\Redis::class,
    ] as $class) {
        $adapter = (new ReflectionClass($class))->newInstanceWithoutConstructor();
        if (!$adapter instanceof SessionIdInterface) {
            throw new RuntimeException($class . ' does not implement SessionIdInterface');
        }
        $ids[] = $requireSid($adapter->create_sid());
        $ids[] = $requireSid($adapter->create_sid());
    }
    if (count(array_unique($ids)) !== count($ids)) {
        throw new RuntimeException('Phalcon session IDs were reused');
    }

    if (session_status() !== PHP_SESSION_NONE || session_module_name() === 'user') {
        throw new RuntimeException('Phalcon SID check requires an inactive default session handler');
    }
    $previousModule = session_module_name();
    $previousId = session_id();
    $previousCookies = ini_get('session.use_cookies');
    $hadSession = array_key_exists('_SESSION', $GLOBALS);
    $previousSession = $hadSession ? $GLOBALS['_SESSION'] : null;

    try {
        if (ini_set('session.use_cookies', '0') === false) {
            throw new RuntimeException('Failed to configure the Phalcon SID check');
        }
        $handler = new class extends Phalcon\Session\Adapter\Noop {
            public int $sidCalls = 0;

            public function create_sid(): string {
                ++$this->sidCalls;
                return parent::create_sid();
            }

            public function validateId(string $id): bool {
                // An empty test store has no existing IDs to collide with.
                return false;
            }
        };
        if (!session_set_save_handler($handler, false) || !session_start() || $handler->sidCalls !== 1) {
            throw new RuntimeException('Phalcon session start did not create exactly one SID');
        }
        $active = $requireSid(session_id());
        $next = $requireSid(session_create_id());
        if ($handler->sidCalls !== 2 || $active === $next) {
            throw new RuntimeException('Phalcon active SID callback recursed or reused an ID');
        }
        $direct = $requireSid($handler->create_sid());
        if ($handler->sidCalls !== 3 || count(array_unique([$active, $next, $direct])) !== 3) {
            throw new RuntimeException('Phalcon direct SID creation recursed or reused an ID');
        }
    } finally {
        try {
            if (session_status() === PHP_SESSION_ACTIVE && !session_write_close()) {
                throw new RuntimeException('Failed to close the Phalcon SID check session');
            }
            if (session_module_name($previousModule) === false || session_id($previousId) === false) {
                throw new RuntimeException('Failed to restore the Phalcon SID check session state');
            }
        } finally {
            if ($hadSession) {
                $GLOBALS['_SESSION'] = $previousSession;
            } else {
                unset($GLOBALS['_SESSION']);
            }
            if (ini_set('session.use_cookies', $previousCookies) === false) {
                throw new RuntimeException('Failed to restore the Phalcon SID check configuration');
            }
        }
    }
})();
